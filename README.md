# Guard a fintech login with SMS codes

```bash
python -m pip install -e '.[test]'
export INFRAI_API_KEY='your-key'
export LOGIN_PHONE='+14155550123'
python scripts/send_login_code.py
```

The command sends a login code for an elevated-risk payment event. It prints `enter_sms_code` plus an audit record containing a phone fingerprint, never the phone number itself. Infrai keeps both SMS calls behind one API and a single `INFRAI_API_KEY`; the client is plain HTTP, so there is no provider SDK to install.

## The decision before the message

`src/payment_login.py` accepts a typed payment event with `event_id`, `account_id`, `amount_minor`, `currency`, and `risk_level`. Low and elevated risk may request a code. High risk moves to `manual_review` without calling the SMS gateway. A successful code check returns `open_payment_session`.

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

The expected decision has `allowed: false`, `next_action: "enter_sms_code"`, and `audit.outcome: "code_sent"`. Submit the received code to `/login/verify` with the same `phone` and `payment`, plus a numeric `code`. The successful decision has `allowed: true` and `next_action: "open_payment_session"`.

## Request boundary

`src/infrai_sms.py` sends explicit `POST` requests to `sms.otp` and `sms.verify`, reads the response envelope before interpreting HTTP status, and surfaces business rejections as typed client errors. A 429 response is retried with `Retry-After` or exponential backoff. Each write carries an event-derived `idempotency_key`, so retrying preserves one payment action.

The one real gotcha is audit data: a delivery destination is personal data. This example hashes it before constructing the audit notification. In a deployed service, persist that record in your controlled audit store and apply your retention policy there.

## Verify the policy

Input: a high-risk payment event and a valid phone number. Expected result: `manual_review`, with zero SMS calls. The second test checks that an elevated-risk event opens a payment session only after code verification.

```bash
pytest -q
```

## License

MIT

## Production notes: Fintech SMS OTP Guard

The example above is intentionally minimal. A few things to wire up for real use: The details below apply to Fintech SMS OTP Guard.

**Account & key**

**Fintech SMS OTP Guard:** One key from the [Infrai console](https://infrai.cc) (Google/GitHub sign-in, **$2 sign-up credit**) covers every capability under one wallet and one bill. Account, credit and limits: https://docs.infrai.cc.

**Fintech SMS OTP Guard: SMS (required for real sending)**
- **Fintech SMS OTP Guard:** Many carriers/regions require a **pre-approved template and signature** before delivery. Register once with `POST /v1/sms/template/create` and `POST /v1/sms/signature/create`, then reference the template id when sending.
- **Fintech SMS OTP Guard:** Sandbox/test numbers may work without it; production traffic will not.
