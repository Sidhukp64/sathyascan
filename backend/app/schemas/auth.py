"""
Dashboard auth request/response schemas (Phase 6, api-design.md's
`/auth/link/start`, `/auth/link/verify`, `/auth/refresh`, `/auth/logout`,
`GET /me`). Never includes a phone number in any response body
(decisions.md §7) — `phone_number` only ever appears as REQUEST input, never
echoed back.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OtpStartRequest(BaseModel):
    phone_number: str = Field(min_length=5, max_length=20)


class OtpStartResponse(BaseModel):
    message: str
    expires_in_seconds: int


class OtpVerifyRequest(BaseModel):
    phone_number: str = Field(min_length=5, max_length=20)
    otp_code: str = Field(min_length=4, max_length=10)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int


class LogoutResponse(BaseModel):
    message: str = "Logged out."


class MeResponse(BaseModel):
    """NEVER phone_number_encrypted or a decrypted phone number — identifies
    the user by user_id only, per decisions.md §7 and api-design.md's `GET
    /me` note."""

    user_id: UUID
    preferred_language: str
    auto_detect_language: bool
    privacy_mode: bool
    created_at: datetime
    last_active_at: datetime
