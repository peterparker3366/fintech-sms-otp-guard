from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .infrai_sms import InfraiError, InfraiSmsClient


class RiskLevel(StrEnum):
    LOW = "low"
    ELEVATED = "elevated"
    HIGH = "high"


class PaymentEvent(BaseModel):
    event_id: str = Field(min_length=3, max_length=80)
    account_id: str = Field(min_length=3, max_length=80)
    amount_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    risk_level: RiskLevel


class OtpStartRequest(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    payment: PaymentEvent


class OtpVerifyRequest(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    payment: PaymentEvent
    code: str = Field(pattern=r"^\d{4,8}$")


class AuditNotification(BaseModel):
    event_id: str
    account_id: str
    phone_fingerprint: str
    action: str
    outcome: str
    recorded_at: datetime


class LoginDecision(BaseModel):
    allowed: bool
    next_action: str
    audit: AuditNotification


def phone_fingerprint(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


def audit_for(request: OtpStartRequest | OtpVerifyRequest, action: str, outcome: str) -> AuditNotification:
    return AuditNotification(
        event_id=request.payment.event_id,
        account_id=request.payment.account_id,
        phone_fingerprint=phone_fingerprint(request.phone),
        action=action,
        outcome=outcome,
        recorded_at=datetime.now(UTC),
    )


def start_login(request: OtpStartRequest, sms: Any) -> LoginDecision:
    if request.payment.risk_level is RiskLevel.HIGH:
        return LoginDecision(
            allowed=False,
            next_action="manual_review",
            audit=audit_for(request, "otp_request", "blocked_by_risk"),
        )
    sms.request_otp(phone=request.phone, request_id=f"otp:{request.payment.event_id}")
    return LoginDecision(
        allowed=False,
        next_action="enter_sms_code",
        audit=audit_for(request, "otp_request", "code_sent"),
    )


def verify_login(request: OtpVerifyRequest, sms: Any) -> LoginDecision:
    if request.payment.risk_level is RiskLevel.HIGH:
        return LoginDecision(
            allowed=False,
            next_action="manual_review",
            audit=audit_for(request, "otp_verify", "blocked_by_risk"),
        )
    sms.verify_otp(
        phone=request.phone,
        code=request.code,
        request_id=f"verify:{request.payment.event_id}",
    )
    return LoginDecision(
        allowed=True,
        next_action="open_payment_session",
        audit=audit_for(request, "otp_verify", "verified"),
    )


def sms_client() -> InfraiSmsClient:
    return InfraiSmsClient()


app = FastAPI(title="Payment login SMS verification")


@app.post("/login/otp", response_model=LoginDecision)
def request_login_otp(body: OtpStartRequest, sms: InfraiSmsClient = Depends(sms_client)) -> LoginDecision:
    try:
        return start_login(body, sms)
    except InfraiError as exc:
        status = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise HTTPException(status_code=status, detail={"code": exc.code}) from exc


@app.post("/login/verify", response_model=LoginDecision)
def confirm_login_otp(body: OtpVerifyRequest, sms: InfraiSmsClient = Depends(sms_client)) -> LoginDecision:
    try:
        return verify_login(body, sms)
    except InfraiError as exc:
        status = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise HTTPException(status_code=status, detail={"code": exc.code}) from exc
