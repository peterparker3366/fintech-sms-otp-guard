# Clinical Password Reset Email API: Audit Evidence After 429 Rate Limits

A healthtech password-reset flow has a constraint that changes the usual retry advice: the team must preserve credible compliance evidence without retaining the reset secret. The right design is to separate the user's repeat-request cooldown, the provider's `429` recovery clock, and the reset token's short validity window. Treating those as one timer either creates duplicate mail or makes a valid request disappear.

**TL;DR:** Accept a reset request without revealing whether the account exists, record one redacted intent, and enqueue delivery under a stable operation ID. On `429`, honor a valid `Retry-After` value, apply bounded jitter, and reschedule the same operation rather than minting a new token or sending inline. A repeated request inside the cooldown should return the same public response and attach to the existing intent. The audit trail should explain what the system decided, when, and under which policy version, while never storing the raw token or reset URL.

## How should a password reset email API handle a 429 rate limit?

There are three clocks because they protect different boundaries. The public resend cooldown limits how often one subject can provoke work. The delivery retry clock protects the downstream transport after it declines work. The expiry clock limits how long the credential-bearing link can be used. None can safely stand in for another.

Suppose the policy allows a 10-minute reset link and a 60-second request cooldown. A delivery adapter receives a `429` and a valid delay of 90 seconds. Retrying after 60 seconds would ignore downstream backpressure; discarding the operation at 60 seconds would confuse a user who has not received anything. The worker should preserve the operation, wait for the downstream delay, and then check whether enough validity remains to justify sending. That final threshold is a policy decision and belongs in the evidence record.

This is the subtle part: **a retry is transport work, not a new security event**. Reissuing the token on every delivery attempt extends or fragments the security window and makes the audit history hard to explain. Keep a single logical intent and let its delivery attempts evolve beneath it.

Short expiry is useful only when every clock is explicit.

## Model evidence before retries

A useful evidence record answers four questions: what intent was accepted, which policy governed it, what delivery decision followed, and whether the credential was ultimately consumed or expired. It does not need message content. In a clinical account, message bodies and URLs are liabilities in logs because they can expose identity context or a live bearer secret.

Store a keyed, non-reversible subject reference; a random operation ID; token fingerprint; policy version; timestamps; adapter outcome class; attempt count; and the parsed retry deadline. Keep the destination in a separately controlled system if operations truly need it. Avoid logging provider response bodies by default because they can echo addresses or request data.

The public endpoint should give the same response for an unknown address, a known address inside cooldown, and a newly accepted request. Internally those are distinct decisions. Externally they should not become an account-discovery channel.

No new job.

A cooldown hit is an evidence event attached to the existing intent.

For marketing mail, RFC 8058 defines a one-click unsubscribe mechanism and specifically discusses DKIM-covered list headers. A password reset is a different message class. Do not copy promotional unsubscribe machinery into an account-recovery transaction merely because both travel by email; classify the stream first, then apply the controls appropriate to that purpose.

## A retry state machine with one logical intent

The following Python example keeps protocol parsing at the adapter boundary and returns a scheduling decision. It is deliberately small: persistence, encryption, and the actual sender belong behind interfaces that can be tested independently.

```python
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from random import uniform


@dataclass(frozen=True)
class RetryDecision:
    operation_id: str
    run_at: datetime | None
    outcome: str


def retry_at(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(now.timestamp() + int(value), timezone.utc)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def decide_after_429(
    operation_id: str,
    header_value: str | None,
    now: datetime,
    expires_at: datetime,
    attempt: int,
) -> RetryDecision:
    provider_deadline = retry_at(header_value, now)
    fallback_seconds = min(15 * (2 ** attempt), 300) + uniform(0, 5)
    candidate = provider_deadline or datetime.fromtimestamp(
        now.timestamp() + fallback_seconds, timezone.utc
    )

    if candidate >= expires_at:
        return RetryDecision(operation_id, None, "expired_before_retry")
    return RetryDecision(operation_id, candidate, "scheduled_same_intent")
```

The database update around this decision must be conditional. Consider two workers that read attempt version 3 just before a lease expires. Worker A receives a `429` and computes a retry deadline. Worker B times out after the downstream system may have accepted the request. If both can update the row without comparing the version, one may schedule attempt 4 while the other marks attempt 3 accepted; the resulting history can't prove whether the second message was authorized. A compare-and-set update lets only the current owner move the operation from `sending` to `waiting`, while a uniqueness constraint on the logical operation ID prevents another outbox row from representing the same user action. Use a separate attempt ID for each adapter call. The intent then has one legible sequence, including the uncomfortable ambiguous result, instead of several rows that happen to share an address.

Do not retry every failure.

Authentication and malformed-request failures need operator attention, not backoff. A timeout is ambiguous: the downstream system may have accepted the message before the connection failed. The adapter therefore needs an idempotency mechanism when its transport supports one, or the worker must reconcile status before another send. If neither is available, record the duplicate risk explicitly and cap attempts.

This design has limits. A short-lived token may expire during a long downstream throttle, while issuing the token only after transport capacity returns complicates the handoff between security and delivery systems. Keeping audit events lean also reduces diagnostic detail. Those are explicit trade-offs: choose the validity threshold and recorded fields from the recovery risk model, and abandon delivery when the remaining lifetime can't support a safe user interaction.

## What should the audit trail prove?

Compliance evidence should be useful without becoming a second credential store. A compact event vocabulary is enough: `intent_accepted`, `cooldown_reused`, `delivery_started`, `delivery_deferred`, `delivery_accepted`, `delivery_abandoned`, `token_consumed`, and `token_expired`. Each event carries the operation ID, event time, policy version, actor class, and reason code. The downstream response class may be recorded; the raw body should not be.

Make the log append-only at the application boundary and define retention by data class. Access to it should itself be auditable. Clock synchronization matters because a sequence whose timestamps disagree across the API, queue, and worker cannot establish much. These controls cost engineering time and some storage, but they produce evidence that can answer an incident review without exposing a usable reset link.

Testing should follow the clocks. A fake clock should cover a numeric `Retry-After`, a date value, a malformed value, a deadline beyond token expiry, concurrent repeat clicks, and two workers claiming one row. Then test the public response separately to prove that account existence and cooldown state do not alter its observable shape. A delivery sandbox cannot prove those properties.

SMS fallback deserves its own threat and consent analysis. The WebOTP API can help a browser receive an SMS-formatted one-time code after user consent, but it does not turn an emailed reset link into an SMS credential or justify silently changing channels. Recovery factors, destination changes, and channel fallback should remain distinct policy decisions.

## Roll out by observing decisions first

Start by emitting the new evidence events alongside the existing path, with token and destination fields blocked from logging. Compare counts of accepted intents, cooldown reuse, downstream deferrals, expiry-before-retry, and successful consumption. The point is reconciliation, not a vanity delivery percentage.

Next, move scheduling behind the outbox for a small traffic slice while keeping the public response stable. Exercise forced `429` responses and ambiguous timeouts in a controlled environment. Increase traffic only after every accepted intent reaches one terminal state and duplicate adapter calls remain explainable through attempt IDs.

Last, enforce the new cooldown and expiry decision centrally, then remove the old inline-send branch. Keep the policy version on historical events. **The durable result is one recoverable user action, one bounded credential lifetime, and one audit narrative even when delivery is throttled.**

## Sources

- https://datatracker.ietf.org/doc/html/rfc8058
- https://developer.mozilla.org/en-US/docs/Web/API/WebOTP_API
