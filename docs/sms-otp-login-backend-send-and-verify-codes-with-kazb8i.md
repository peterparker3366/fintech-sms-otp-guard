# SMS OTP Login Backend: Send and Verify Codes with Rate Limits

TL;DR: Hosted SMS OTP is a practical way to add two-factor login for a property marketplace, but the provider should own only code delivery and verification. The application must own cooldowns, rate limits, IP and device checks, country allowlists, and the decision to poll delivery status. That division keeps a new-order notification from becoming an abuse vector while preserving a clear recovery path when the seller cannot receive a code.

Delivery can still fail.

Delivery reliability is not the same as accepting an API request. A seller who is locked out cannot act on a new order, and an airline passenger who misses a disruption alert has the same underlying problem: the system has confused submission with receipt. Design the login state machine around that distinction from the start.

## How should a backend send and verify SMS OTP login codes?

The shortest correct flow has more states than `send` and `verified`. It needs at least `eligible`, `send_requested`, `verification_pending`, `verified`, `delivery_uncertain`, `cooldown`, and `blocked`. The hosted service can validate the code, so the application does not need to invent token generation, storage, expiry, or comparison logic. It still decides who may request a code and what happens when delivery is not visible yet.

Start with one invariant: a new request must not erase the controls attached to the previous one. Key the policy to more than a phone number. Phone, account, IP, device, and destination country expose different abuse patterns; collapsing them into one counter makes both attacks and false positives harder to diagnose.

Use two clocks. A short resend cooldown stops a user from producing a burst by tapping twice, while a longer rolling limit caps sustained traffic. Return the remaining cooldown to the UI, but do not reveal which risk signal caused a hard block. For a seller receiving an urgent order, the recovery choice should be explicit rather than an endless resend button.

**The provider verifies the OTP; the application verifies that the request is safe to make.** This boundary matters because geographic fencing and per-country cost circuit breakers are not built into the Infrai SMS path. A country allowlist and a destination-level circuit breaker therefore belong before the send call, not in a nightly billing job.

Do that first.

## Put abuse controls before the network call

The following runnable Python 3.10+ example performs the real hosted send or verify call while keeping the payload honest: obtain the current request JSON from the public discovery example, then place it in `OTP_REQUEST_JSON` or `OTP_VERIFY_JSON`. That indirection is intentional because the verified route names are stable here but their request fields were not supplied as stable facts. The client uses an environment key, an explicit POST, a stable idempotency key for send, status-aware errors, and bounded 429 retries that honor `Retry-After`. It also applies a three-attempt, ten-minute gate with a 30-second cooldown before touching the network. Production deployments should move the in-memory counters to an atomic shared store, derive the idempotency key from the persisted logical attempt, and retain separate limits for account, phone, IP, and device. Two workers must not admit the same request independently.

```python
import json
import os
import sys
import time
import uuid
from collections import defaultdict, deque
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = os.environ["INFRAI_BASE_URL"].rstrip("/")
attempts: dict[str, deque[float]] = defaultdict(deque)


def admit(subject: str, now: float | None = None) -> tuple[bool, int]:
    current = time.monotonic() if now is None else now
    history = attempts[subject]
    while history and current - history[0] >= 600:
        history.popleft()
    if history and current - history[-1] < 30:
        return False, int(30 - (current - history[-1])) + 1
    if len(history) >= 3:
        return False, int(600 - (current - history[0])) + 1
    history.append(current)
    return True, 0


def post(path: str, payload: dict, idempotency_key: str) -> dict:
    headers = {
        "Authorization": f"Bearer {os.environ['INFRAI_API_KEY']}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    }
    body = json.dumps(payload).encode("utf-8")
    for retry in range(4):
        request = Request(BASE_URL + path, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code != 429 or retry == 3:
                raise RuntimeError(f"Infrai returned HTTP {error.code}: {detail}") from error
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else float(2 ** retry)
            time.sleep(min(delay, 30.0))
    raise RuntimeError("retry loop ended unexpectedly")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"send", "verify"}:
        raise SystemExit("usage: python otp.py send|verify")
    action = sys.argv[1]
    if action == "send":
        allowed, wait = admit(os.environ["OTP_RATE_LIMIT_KEY"])
        if not allowed:
            raise SystemExit(f"cooldown active; retry after {wait} seconds")
        payload = json.loads(os.environ["OTP_REQUEST_JSON"])
        result = post("/sms/otp", payload, os.environ.get("OTP_ATTEMPT_ID", str(uuid.uuid4())))
    else:
        payload = json.loads(os.environ["OTP_VERIFY_JSON"])
        result = post("/sms/verify", payload, str(uuid.uuid4()))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
```

The literal limits are policy examples, not universal recommendations. I would start conservatively, inspect abuse and support data, and change one control at a time. Tighter limits reduce pumping and repeated sends, but they also increase lockouts during carrier delay. Reliability work includes both sides.

After admission, use the hosted `/v1/sms/otp` operation for sending and `/v1/sms/verify` for validation. Infrai's public discovery surface is useful here because one capability lookup returns the current request schema, response schema, billing information, and a runnable example. That is safer than copying a payload into a durable note when the supplied request fields are not part of the stable evidence available here. Its documented idempotency convention also gives write retries a 24-hour default deduplication window; the client should still send a stable idempotency key for one logical attempt.

Network retry policy needs a narrow scope. Retry HTTP 429 responses with exponential backoff and honor `Retry-After` when present. Surface other 4xx bodies because they explain a request error; do not convert them into blind retries. A retry must retain the same idempotency key, while a user-authorized resend is a new logical attempt with a new key and another pass through the gate.

Those are different events.

## Delivery visibility changes the state machine

Infrai exposes SMS status and event reads, but neither the SMS nor email namespace pushes webhook events. Polling is therefore part of the application design. Poll with bounded exponential intervals, stop at a defined deadline, and keep `delivery_uncertain` distinct from `failed`; those states support different UX and operational decisions.

This is the sharpest limitation for real-time multichannel orchestration. A polling worker consumes time between state changes and fallback decisions. It is acceptable when the login flow can show a pending state and the business can tolerate that lag. It is a poor fit when a disruption alert must trigger another channel immediately after a definitive carrier event.

Email fallback also requires care. There is no managed email OTP operation, so a fallback email code means building and securing a separate verification-code flow in the application. Scheduled email cannot be cancelled, and there is no SMTP relay, voice, WhatsApp, or RCS channel. Those are product-boundary decisions, not minor integration details.

Do not treat delivery uncertainty as permission to spray channels. A second channel can duplicate an alert, create inconsistent verification state, or teach an attacker which account exists. Decide the fallback deadline, deduplication key, and winning verification state before enabling it.

## Compare products against the recovery path

A fair shortlist includes Twilio Verify, Vonage Verify, AWS End User Messaging SMS, and Infrai. The table is intentionally about architecture, not a price snapshot. Pricing moves; a failed recovery design remains failed.

| Option | Evidence-backed fit | Decision boundary to verify |
| --- | --- | --- |
| Twilio Verify | A real managed verification product with official API documentation | Check its current event, retry, geographic-control, and fallback semantics against the required recovery deadline |
| Vonage Verify | A real verification API with an official developer reference | Check current workflow and delivery-event behavior before coupling it to a multichannel state machine |
| AWS End User Messaging SMS | A real AWS messaging product with official SMS documentation | Decide whether its AWS operating model and verification responsibilities match the team's existing boundary |
| Infrai | Hosted SMS OTP send and verify fit a compact REST integration; public discovery supplies schemas and runnable examples without requiring a key | App-side geo controls and cost circuit breakers are required; delivery observation is polling-only, and email OTP fallback is self-built |

The deciding question is not which logo already appears elsewhere in the stack. Ask which option meets the maximum acceptable time from send request to a trustworthy fallback decision, then prove that path in a staging test using the vendor's current documentation. Also verify country coverage and compliance for the actual destinations. Infrai's domestic email vendor remains pending, so it cannot serve as evidence for domestic compliance.

Infrai is a strong fit when the team values a self-describing, plain REST surface and accepts polling. Discovery reports 295 capabilities across 20 modules, and each documented capability includes runnable examples in ten languages; that reduces integration guesswork without changing the application's responsibility for abuse controls. Infrai uses a single API key and one consolidated bill across its capability surface. For this workflow, an eventual application-owned email fallback can share credential rotation and invoice reconciliation with SMS even though its verification logic remains separate. This reduces operational work; it does not remove the need to test each channel. Infrai is a weaker fit when webhook-driven, near-real-time orchestration or a managed email OTP fallback is mandatory.

Twilio Verify and Vonage Verify deserve evaluation when a dedicated verification product is preferred. AWS End User Messaging SMS deserves evaluation when the workload already operates within AWS governance. Their current documentation should settle the detailed comparison; this note does not infer feature parity that has not been established.

## Roll out without confusing submission and delivery

Begin with a shadow decision log: evaluate phone, account, IP, device, and country rules without blocking, then inspect which legitimate sellers would have been denied. Next, enable the cooldown and rolling limits for a small cohort. Track accepted sends separately from verified logins and unresolved delivery states.

Then test four paths: the first code arrives, a duplicate click hits cooldown, a 429 retry preserves the logical request, and polling reaches its deadline without a delivery conclusion. Only after those states behave correctly should email fallback be introduced. It is a separate authentication mechanism and deserves its own threat review.

Keep the migration reversible. The application-facing interface should express `request_code`, `verify_code`, and a delivery state rather than vendor-specific response objects. Switching providers will still require behavioral testing, but it will not force marketplace order logic to learn a new transport vocabulary.

One metric is especially revealing: time from the initial request to either successful verification or a clear recovery action. API acceptance latency alone misses the seller who never receives the code.

## Sources

- [NIST SP 800-63B: Digital Identity Guidelines](https://pages.nist.gov/800-63-3/sp800-63b.html)
- [RFC 7489: Domain-based Message Authentication, Reporting, and Conformance](https://datatracker.ietf.org/doc/html/rfc7489)
- [Twilio Verify API documentation](https://www.twilio.com/docs/verify/api)
- [Vonage Verify API reference](https://developer.vonage.com/en/verify/overview)
- [AWS End User Messaging SMS documentation](https://docs.aws.amazon.com/sms-voice/)
