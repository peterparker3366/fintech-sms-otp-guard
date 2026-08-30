# Template Ownership Decides How You Diagnose a Password Reset Email API Timeout

The trade-off that decides everything else in a password reset flow is who owns the email template — your repository or the provider's dashboard — because that one choice determines whether a hanging send is something you can diagnose from your own logs or something you have to reconstruct from someone else's. In short: bound the outbound API request with an explicit deadline, keep the token's expiry clock independent of that deadline, and treat template ownership as the variable that decides how much of a delayed send you can explain afterwards.

A client timeout is a fact about your process. It says your Node.js service stopped waiting. It says nothing about whether the message was accepted, rendered, queued, or already sitting in somebody's inbox.

Two clocks, two owners.

## A 15-minute expiry turns delivery latency into a correctness bug

Take a B2B SaaS admin console where an account owner triggers a password reset for a teammate and the reset link dies 15 minutes after it is issued. That expiry is a security decision, not a UX preference, and shortening it is usually the right call. Delivery latency then spends the same budget. If the transactional email provider holds the message for eleven minutes behind a queue you can't see, your teammate opens a link with four minutes of life left, and the support queue fills with "the link says expired" reports that look like a token bug and are really a delivery bug.

So the first design question is not which provider to integrate. It is when the clock starts.

Starting expiry at issuance is simple and auditable: one timestamp, one row, no dependence on anything outside your database. Starting it when the provider acknowledges acceptance is kinder to the user, but it requires an acceptance signal you trust — which is precisely the signal a hanging request denies you. I'm not sure there's a universally correct answer here; it depends on how much of your reset traffic goes to domains with aggressive greylisting. What I'd avoid is the middle ground where a background job silently extends tokens whenever delivery was slow, because now expiry is a function of provider behavior and nobody can reason about the security property anymore.

Set the deadline explicitly in whichever client you use. Axios' `timeout` option defaults to `0`, meaning no timeout at all, and it is measured against the response rather than the whole exchange. The global `fetch` in Node.js has no total request budget until you pass one, so an `AbortSignal.timeout(10_000)` on the send call is the difference between a bounded handler and a handler that can be parked for minutes by a slow upstream. Ten seconds is defensible for a user-facing submission. The retry budget belongs somewhere else entirely.

## What should a Node.js service do when the transactional email API request hangs?

Return the same neutral response you would have returned on success, and move the delivery problem off the request path. The reason is not politeness — a response that differs by whether the address exists is an account enumeration oracle, and so is a response whose latency differs. If a request only times out for real accounts, because only real accounts trigger an actual send, you have built a timing side channel into your recovery flow. Answer in constant shape and, as far as you reasonably can, constant time.

Behind that response, the state machine matters more than the transport. Write the attempt row before the call, with an idempotency key you generate, and let a worker own the outcome. A workable set of states is `pending`, `accepted`, `delivered`, `bounced`, and `unknown`. Collapsing a timeout into `failed` is the mistake that produces the ugliest incidents, because `failed` invites an automatic second attempt while the first message may already be moving through delivery. Two valid reset links then race each other into the same inbox, arriving out of order, and the user clicks the older one.

No blind second sends.

Rate limiting deserves a line here too. Cap attempts per account and per destination address, not just per IP, or a user mashing the button produces six live tokens with six different expiry stamps. Decide explicitly whether reissuing invalidates the previous token — either policy is defensible, but only one of them can be true, and it needs to be written down where the support team can read it.

## Reconciling an unknown send without a retry

Reconciliation is a lookup, never a second delivery attempt. The worker asks the provider what happened to the attempt it already has a key for, backs off, and stops when the answer is terminal or the window closes.

```python
import os
import time

import requests

API = os.environ["EMAIL_API_BASE"].rstrip("/")
AUTH = {"Authorization": f"Bearer {os.environ['EMAIL_API_TOKEN']}"}

# (connect, read): a read timeout alone still lets a black-holed route
# hold the socket while TCP politely retransmits.
TIMEOUTS = (3.05, 10)
TERMINAL = {"delivered", "bounced", "dropped", "rejected"}


def reconcile(attempt_key: str, window_s: float = 900.0) -> str:
    """Resolve one unknown send. Reads only; never triggers a delivery."""
    started = time.monotonic()
    backoff = 2.0
    while time.monotonic() - started < window_s:
        r = requests.get(
            f"{API}/messages",
            params={"metadata.attempt_key": attempt_key, "limit": 1},
            headers=AUTH,
            timeout=TIMEOUTS,
        )
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", backoff)))
            backoff = min(backoff * 2, 60.0)
            continue
        r.raise_for_status()
        items = r.json().get("data", [])
        if items and items[0]["status"] in TERMINAL:
            return items[0]["status"]
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)
    return "unknown"
```

The catch is the filter on line one of that request. This pattern only works if the provider lets you search by something you control — a metadata field, a tag, a custom header echoed back on the message record. Several do; some expose lookup by their own message id and nothing else, which is useless when the response carrying that id is the thing you lost. If your provider doesn't support a caller-owned key, you are correlating on recipient address plus a timestamp range, and under load that is guesswork rather than reconciliation. Test it before you depend on it: cut the connection deliberately after the request leaves, then see whether the worker can find the message.

An attempt that stays `unknown` past the window is an operations problem, not a user problem. Page on the age and count of unresolved attempts. A timeout that reconciles to `delivered` two seconds later is noise; a hundred attempts stuck at `pending` is a queue backing up somewhere upstream, and the two look identical if all you graph is client-side error counts.

## Three ownership models, and the audit data each one leaves you

Everything above assumes you can reproduce the message that was supposed to go out. That is where ownership stops being an organizational preference and becomes an engineering constraint.

| Ownership model | What you send | What you can prove after a timeout |
|---|---|---|
| Rendered in your repo | Final subject and body | Byte-for-byte content, from a hash you logged yourself |
| Provider-hosted template | Template id plus variables | The variables you sent; rendering is opaque to you |
| Repo is source of truth, synced on deploy | Template id plus variables | Content, if the synced version id is recorded per send |

With in-repo rendering, a hanging request has exactly one unknown: transport. You already logged the rendered body's hash, so you can replay the same bytes against a test endpoint, diff them against what the user eventually received, and settle the argument in minutes. With a hosted template, the render happens on the far side of the call you lost, and the payload in your logs no longer reproduces the message. That gap is manageable, but only if you record the template version identifier with every attempt — otherwise a colleague editing copy in a dashboard at 14:00 has silently changed the artifact you are trying to debug at 14:05, with no diff and no author.

Which brings up the compliance side of the same axis. A password reset message is security-critical text: it states an expiry, it tells users what to do if they didn't request it, and in regulated accounts it may need retained evidence of what was sent. In-repo templates inherit code review, git blame and release tagging for free. Dashboard-owned templates need an approval workflow bolted on, or you have unreviewed production changes to a security message with no audit trail — the sort of thing that turns into a finding during a customer's vendor assessment.

In-repo rendering is a bad fit when copy changes daily across a dozen locales and non-engineers must ship those changes without waiting for a deploy. Stick with hosted templates in that case, and buy back the observability you gave up: version identifier per send, variable snapshot per attempt, and a nightly job that pulls each live template and diffs it against the last approved copy.

The fallback channel raises the same question under a harder constraint. Reset codes over SMS are encoded in GSM-7 at 160 characters per single message, but one character outside that alphabet flips the whole body to UCS-2 at 70 characters, and a marketer pasting a curly apostrophe into a hosted template can turn a one-part message into three parts — more cost, more segments to reassemble, more ways for a code to arrive scrambled on an old handset. That check belongs in CI, wherever the template lives.

```python
GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
EXTENDED = set("^{}\\[~]|€")  # each of these costs two GSM-7 characters


def segments(body: str) -> int:
    if not set(body) <= GSM7 | EXTENDED:
        return -(-len(body) // 70) if len(body) > 70 else 1
    units = sum(2 if c in EXTENDED else 1 for c in body)
    return 1 if units <= 160 else -(-units // 153)
```

## A rollout that doesn't need a rewrite

Bound the send call with an explicit deadline and return a neutral response. Add the attempt table with a caller-owned idempotency key, and point your existing send path at it. Move reconciliation into a worker with a bounded window. Then record the template version identifier — or the rendered body hash, whichever your ownership model gives you — on every attempt, because that field is what makes the previous three steps worth having.

Verify it with a drill, not a diagram. Interrupt the connection mid-submission on purpose in staging and confirm three things: the user got a normal-looking response, the worker resolved the attempt without sending anything, and you can still say what the message contained.

The rest is the boring part of deliverability that decides whether any of this reaches an inbox: an authenticated sending domain, reset mail on a stream separate from marketing, no link wrapping that rewrites your token URL through a redirect domain, and no unsubscribe machinery grafted onto a message nobody opted into. RFC 8058 governs one-click unsubscribe for bulk mail, and a password reset is not bulk mail. Keeping those two categories apart, in code and in whoever's dashboard, is worth more than any amount of client-side retry logic.

## Sources

- OWASP Cheat Sheet Series, Forgot Password Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
- MDN Web Docs, AbortSignal.timeout(): https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/timeout_static
- Axios request config reference: https://github.com/axios/axios#request-config
- Node.js HTTP API, request timeouts: https://nodejs.org/api/http.html
- Twilio, SMS character limits and segmentation (GSM-7/UCS-2): https://www.twilio.com/docs/glossary/what-sms-character-limit
- RFC 8058, One-Click Unsubscribe: https://datatracker.ietf.org/doc/html/rfc8058
