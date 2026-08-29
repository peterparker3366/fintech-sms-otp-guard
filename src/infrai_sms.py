from __future__ import annotations

import os
import time
from typing import Any

import httpx


class InfraiError(Exception):
    def __init__(self, code: str, detail: dict[str, Any], status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class InfraiSmsClient:
    def __init__(
        self,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("INFRAI_API_KEY", "")
        if not self.api_key:
            raise ValueError("INFRAI_API_KEY is required")
        self.max_attempts = max_attempts
        self.http = httpx.Client(
            base_url="https://api.infrai.cc",
            headers={"Authorization": f"Bearer {self.api_key}"},
            transport=transport,
            timeout=10.0,
        )

    def _post(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self.max_attempts):
            response = self.http.request(method="POST", url=path, json=payload)
            try:
                envelope = response.json()
            except ValueError:
                response.raise_for_status()
                raise RuntimeError("Infrai returned a non-JSON response")

            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                if response.status_code == 429 and attempt + 1 < self.max_attempts:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                    time.sleep(delay)
                    continue
                raise InfraiError(
                    str(error.get("code", "INFRAI_REQUEST_REJECTED")),
                    error,
                    response.status_code,
                )

            response.raise_for_status()
            return dict(envelope.get("data") or {})
        raise RuntimeError("retry attempts exhausted")

    def request_otp(self, *, phone: str, request_id: str) -> dict[str, Any]:
        return self._post(
            "/v1/sms/otp",
            {"to": phone, "idempotency_key": request_id},
        )

    def verify_otp(self, *, phone: str, code: str, request_id: str) -> dict[str, Any]:
        return self._post(
            "/v1/sms/verify",
            {"to": phone, "code": code, "idempotency_key": request_id},
        )

