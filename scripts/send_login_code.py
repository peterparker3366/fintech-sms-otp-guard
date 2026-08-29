import os

from src.payment_login import OtpStartRequest, PaymentEvent, RiskLevel, start_login
from src.infrai_sms import InfraiSmsClient


def main() -> None:
    phone = os.environ.get("LOGIN_PHONE", "")
    if not phone:
        raise SystemExit("LOGIN_PHONE is required")
    request = OtpStartRequest(
        phone=phone,
        payment=PaymentEvent(
            event_id="demo-payment-001",
            account_id="demo-account-001",
            amount_minor=2500,
            currency="USD",
            risk_level=RiskLevel.ELEVATED,
        ),
    )
    print(start_login(request, InfraiSmsClient()).model_dump_json(indent=2))


if __name__ == "__main__":
    main()

