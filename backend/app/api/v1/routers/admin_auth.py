"""
Phase 9 admin auth (roadmap §9.1). Email+password login, NOT WhatsApp-OTP —
admins are internal operators, not necessarily reachable via a phone number,
and this deliberately keeps admin identity structurally separate from
end-user identity (see app/models/admin_user.py's docstring).

**No public signup/registration endpoint exists here or anywhere** — an
admin_users row is created only via `scripts/create_admin.py`, run locally
by someone with direct database access. Exposing account creation over
HTTP would itself be the single biggest privilege-escalation risk this
phase could introduce.

Per-IP rate limiting on login (reuses app/core/rate_limit.py, same
mechanism as OTP-start) is real brute-force resistance for a credential
that — unlike a WhatsApp OTP — has no built-in expiry forcing a fresh
guess each time.

Login failures return the SAME generic error whether the email doesn't
exist, the account is inactive, or the password is wrong — never letting a
caller distinguish "no such admin" from "wrong password," the same
enumeration-resistant pattern app/api/v1/routers/auth.py's OTP verify
already uses.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.api.v1.deps import get_current_admin_token_payload, get_db_session, get_redis
from app.core.admin_security import normalize_admin_email, verify_password
from app.core.config import Settings, get_settings
from app.core.jwt_auth import TokenPayload, create_access_token, revoke_token
from app.core.logging import log_event
from app.core.rate_limit import RateLimiter
from app.models.admin_user import AdminUser
from app.schemas.admin_auth import AdminLoginRequest, AdminLoginResponse, AdminLogoutResponse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin-auth"])

_GENERIC_LOGIN_ERROR = "Invalid email or password."


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(
    body: AdminLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> AdminLoginResponse:
    client_ip = request.client.host if request.client else "unknown"
    limiter = RateLimiter(redis, settings.admin_login_rate_limit_per_ip_per_minute)
    rate_result = await limiter.check_and_increment(f"admin-login-ip:{client_ip}")
    if not rate_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts. Please slow down."
        )

    email = normalize_admin_email(body.email)
    result = await session.execute(select(AdminUser).where(AdminUser.email == email))
    admin = result.scalar_one_or_none()

    if admin is None or not admin.is_active or not verify_password(body.password, admin.password_hash):
        log_event(logger, logging.WARNING, "admin login failed", client_ip=client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC_LOGIN_ERROR)

    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is temporarily unavailable.",
        )

    token = create_access_token(
        admin.id, settings.jwt_secret, settings.admin_jwt_access_token_ttl_seconds, token_type="admin"
    )

    admin.last_login_at = datetime.now(timezone.utc)
    await write_audit_log(
        session,
        actor_type="admin",
        action="admin_login",
        entity_type="admin_user",
        entity_id=admin.id,
    )
    await session.commit()

    return AdminLoginResponse(access_token=token, role=admin.role, email=admin.email)


@router.post("/logout", response_model=AdminLogoutResponse)
async def admin_logout(
    payload: TokenPayload = Depends(get_current_admin_token_payload),
    redis: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
) -> AdminLogoutResponse:
    await revoke_token(redis, payload)
    await write_audit_log(
        session,
        actor_type="admin",
        action="admin_logout",
        entity_type="admin_user",
        entity_id=payload.user_id,
    )
    await session.commit()
    return AdminLogoutResponse(message="Logged out.")
