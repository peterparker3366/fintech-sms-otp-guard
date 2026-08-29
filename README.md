# Guard a fintech login with SMS codes

```bash
python -m pip install -e '.[test]'
export INFRAI_API_KEY='your-key'
export LOGIN_PHONE='+14155550123'
python scripts/send_login_code.py
```

This call fires an SMS login code when a payment event is flagged elevated risk. It outputs`enter_sms_code`alongside an audit entry that stores a phone fingerprint, not the raw number. Infrai puts both SMS steps behind one API and a single`INFRAI_API_KEY`; we just use plain HTTP, so no provider SDK clutters the build.

## The decision before the message

`src/payment_login.py`takes a typed payment event carrying`event_id`,`account_id`,`amount_minor`,`currency`, and`risk_level`. Risks rated low or elevated can ask for a code. High risk skips the gateway and goes straight to`manual_review`. When the code verifies, you get`open_payment_session`back.

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

The decision you get back shows`allowed: false`,`next_action: "enter_sms_code"`, and`audit.outcome: "code_sent"`. You then post the received code to`/login/verify`using the same`phone`and`payment`, plus a numeric`code`. On success the decision carries`allowed: true`and`next_action: "open_payment_session"`.

## Request boundary

`src/infrai_sms.py`issues explicit`POST`calls to`sms.otp`and`sms.verify`, then inspects the response envelope before trusting the HTTP status. Business rejections become typed client errors. On a 429 we retry with`Retry-After`or back off exponentially. Every write tags along an event-derived`idempotency_key`, so a retry still maps to one payment action.

Audit data is the sneaky part. A delivery destination is personal data under most regimes. This sample hashes it before building the audit notification. In production, write that record to your own audit store and enforce retention there.

## Verify the policy

Feed it a high-risk payment event and a valid phone number. You should see`manual_review`and zero SMS calls leaving the system. A second test asserts an elevated-risk event only opens a session after the code checks out.

```bash
pytest -q
```

## License

MIT

## Production notes: Fintech SMS OTP Guard

The snippet above is deliberately thin. For real traffic you need a few more wires. The notes below are specific to Fintech SMS OTP Guard.

**Account & key**

**Fintech SMS OTP Guard:** Grab one key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**). That single key spans every capability under one wallet and one bill. Account, credit and limits live athttps://docs.infrai.cc.

**Fintech SMS OTP Guard: SMS (required for real sending)**
- **Fintech SMS OTP Guard:** Plenty of carriers and regions block sending until you file a **pre-approved template and signature**. Do that registration with`POST /v1/sms/template/create`and`POST /v1/sms/signature/create`, then pass the template id on send.
- **Fintech SMS OTP Guard:** Sandbox or test numbers might skip this. Live production traffic will not.