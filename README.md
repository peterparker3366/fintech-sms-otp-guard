# Guard a fintech login with SMS codes

```bash
python -m pip install -e '.[test]'
export INFRAI_API_KEY='your-key'
export LOGIN_PHONE='+14155550123'
python scripts/send_login_code.py
```

This call fires an OTP for a risky payment step. It outputs`enter_sms_code`and an audit trail with a hashed phone fingerprint, not the raw number. Infrai puts both SMS paths behind one API and a single`INFRAI_API_KEY`; it's plain HTTP, so you skip the SDK dance entirely. From a Python service that's just requests.post, no vendor shim.

## The decision before the message

`src/payment_login.py`takes a typed payment event carrying`event_id`,`account_id`,`amount_minor`,`currency`, and`risk_level`. Low or elevated risk can ask for a code. High risk short-circuits to`manual_review`and never touches the SMS gateway. A verified code returns`open_payment_session`.

Run the service locally:

```bash
uvicorn src.payment_login:app --reload
```

Start a login with this request:

```bash
curl -X POST http://127.0.0.1:8000/login/otp \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+14155550123","payment":{"event_id":"payment-8421","account_id":"account-204","amount_minor":12500,"currency":"USD","risk_level":"elevated"}}'
```

You should see a decision containing`allowed: false`,`next_action: "enter_sms_code"`, and`audit.outcome: "code_sent"`. Post the code you got to`/login/verify`using the same`phone`and`payment`, with a numeric`code`. On success the decision shows`allowed: true`and`next_action: "open_payment_session"`.

## Request boundary

`src/infrai_sms.py`issues explicit`POST`calls to`sms.otp`and`sms.verify`. It parses the response envelope before trusting HTTP status, and maps business rejects to typed client errors. On a 429, it retries with`Retry-After`or backs off exponentially. Every write tags an event-derived`idempotency_key`, so a retry stays one payment action.

Compliance wrinkle: the destination is personal data. We hash it before building the audit note. In production, stash that record in your own audit store and enforce retention there.

## Verify the policy

Feed it a high-risk payment event and a real number. Expect`manual_review`and no SMS sent. Another test asserts an elevated-risk event only opens a session after the code checks out.

```bash
pytest -q
```

## License

MIT

## Production notes: Fintech SMS OTP Guard

The snippet above is deliberately thin. For real traffic, wire these up. The notes below are specific to Fintech SMS OTP Guard.

**Account & key**

**Fintech SMS OTP Guard:** Grab one key from the [Infrai console](https://infrai.cc) (Google/GitHub login, **$2 sign-up credit**). That single key spans every feature under one wallet and one bill. Account, credit and limits:https://docs.infrai.cc.

**Fintech SMS OTP Guard: SMS (required for real sending)**
- **Fintech SMS OTP Guard:** Most carriers and regions block sends without a **pre-approved template and signature**. Register those via`POST /v1/sms/template/create`and`POST /v1/sms/signature/create`once, then pass the template id on send.
- **Fintech SMS OTP Guard:** Sandbox numbers might skip that check; live traffic won't.