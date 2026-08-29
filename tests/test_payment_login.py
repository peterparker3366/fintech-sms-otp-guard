from typing import Any

from src.payment_login import (
    OtpStartRequest,
    OtpVerifyRequest,
    PaymentEvent,
    RiskLevel,
    start_login,
    verify_login,
)


class RecordingSms:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request_otp(self, *, phone: str, request_id: str) -> dict[str, Any]:
        self.calls.append(("request", request_id))
        return {"accepted": True}

    def verify_otp(self, *, phone: str, code: str, request_id: str) -> dict[str, Any]:
        self.calls.append(("verify", request_id))
        return {"verified": True}


def payment(risk: RiskLevel) -> PaymentEvent:
    return PaymentEvent(
        event_id="payment-8421",
        account_id="account-204",
        amount_minor=125_00,
        currency="USD",
        risk_level=risk,
    )


def test_high_risk_payment_never_sends_a_code() -> None:
    sms = RecordingSms()
    result = start_login(
        OtpStartRequest(phone="+14155550123", payment=payment(RiskLevel.HIGH)),
        sms,
    )

    assert result.allowed is False
    assert result.next_action == "manual_review"
    assert result.audit.outcome == "blocked_by_risk"
    assert result.audit.phone_fingerprint != "+14155550123"
    assert sms.calls == []


def test_elevated_risk_login_opens_only_after_verification() -> None:
    sms = RecordingSms()
    result = verify_login(
        OtpVerifyRequest(
            phone="+14155550123",
            payment=payment(RiskLevel.ELEVATED),
            code="481902",
        ),
        sms,
    )

    assert result.allowed is True
    assert result.next_action == "open_payment_session"
    assert result.audit.outcome == "verified"
    assert sms.calls == [("verify", "verify:payment-8421")]

