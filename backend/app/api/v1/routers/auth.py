"""
Phase 6 dashboard auth endpoints (docs/api-design.md's `/auth/link/start`,
`/auth/link/verify`, `/auth/refresh`, `/auth/logout`, `GET /me`).

**Confirmed conflict with decisions.md §4, built anyway per explicit user
instruction (AskUserQuestion, Phase 6 planning — "Recommended" option
chosen: build OTP-via-WhatsApp exactly as api-design.md designed it, with
the limitation documented rather than deferring dashboard login
entirely)**: decisions.md §4 is LOCKED as "MVP responses are
synchronous-to-the-conversation only... No proactive/scheduled messaging of
any kind ships in the MVP." Sending an OTP the instant a user starts a
dashboard login is a genuinely PROACTIVE send — not a reply to an inbound
message — which in a REAL Meta deployment requires a pre-approved message
template when the recipient is outside an active 24h conversation window
(same precondition already established for "Check This Tomorrow",
decisions.md §4/§14). This code sends a free-form text via the existing
WhatsAppSender regardless, same as every other reply in this codebase; nothing
here submits or checks for template approval — that is a real-world
production gap, documented, not silently worked around.

**Live-testing status (this environment)**: WHATSAPP_ACCESS_TOKEN is empty
here — every OTP send attempt in this environment WILL fail at the Graph API
call. That failure path (WhatsAppSendError -> 502, audited as
`otp_send_failed`) is exercised by tests; actual WhatsApp message delivery
has NOT been live-tested and must not be described as production-ready
without doing so against real Meta credentials. See backend/README.md.

Never logs an OTP code, a JWT, or a raw/normalized phone number anywhere —
only `phone_hash` (truncated) in log lines, and only `user_id` in audit_log
entries (app/agent/audit.py enforces this defensively).
"""

import hmac
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.agent.user_service import get_or_create_user
from app.api.v1.deps import get_current_token_payload, get_current_user, get_db_session, get_redis
from app.core.config import Settings, get_settings
from app.core.jwt_auth import TokenPayload, create_access_token, revoke_token
from app.core.logging import log_event
from app.core.otp_cooldown import OtpCooldownGuard
from app.core.rate_limit import RateLimiter
from app.core.security import (
    generate_otp_code,
    hash_otp_code,
    hash_phone_number,
    normalize_phone_number,
)
from app.models.dashboard_account import DashboardAccount
from app.models.otp_verification import OtpVerification
from app.models.user import User
from app.schemas.auth import (
    LogoutResponse,
    MeResponse,
    OtpStartRequest,
    OtpStartResponse,
    OtpVerifyRequest,
    TokenResponse,
)
from app.webhook.whatsapp.router import get_sender
from app.webhook.whatsapp.sender import WhatsAppSendError, WhatsAppSender

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["dashboard-auth"])

# Deliberately generic and IDENTICAL across every /auth/link/verify failure
# mode (no active code, expired, max attempts exceeded, wrong code) — does
# not reveal which specific reason applied, so a caller can't use this
# endpoint to enumerate registered phone numbers or probe OTP state.
_GENERIC_OTP_ERROR = "Invalid or expired verification code."


@router.post("/auth/link/start", response_model=OtpStartResponse)
async def start_otp_link(
    payload: OtpStartRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
    sender: WhatsAppSender = Depends(get_sender),
) -> OtpStartResponse:
    # Phase 7 — per-IP rate limit, checked BEFORE the per-phone cooldown.
    # The cooldown alone bounds abuse of any ONE phone number but not an
    # attacker cycling through many different numbers — each real OTP send
    # is a genuine per-message cost in production (WhatsApp Cloud API
    # conversation pricing), so this is a cost control, not just an abuse
    # control, same rationale as decisions.md §1's per-analysis budget guard.
    # request.client.host is used as-is (no X-Forwarded-For handling) — a
    # reverse-proxy deployment must set that up at the proxy layer; see
    # backend/README.md's Phase 7 note.
    client_ip = request.client.host if request.client else "unknown"
    ip_limiter = RateLimiter(redis, settings.otp_start_rate_limit_per_ip_per_minute)
    ip_result = await ip_limiter.check_and_increment(f"otp-start-ip:{client_ip}")
    if not ip_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification code requests from this location. Please try again shortly.",
        )

    normalized = normalize_phone_number(payload.phone_number)
    if normalized is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid phone number.")

    phone_hash = hash_phone_number(normalized, settings.phone_hash_pepper)

    cooldown = OtpCooldownGuard(redis, settings.otp_request_cooldown_seconds)
    if not await cooldown.start_cooldown_if_allowed(phone_hash):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A verification code was already sent recently. Please wait before requesting another.",
        )

    user = await get_or_create_user(
        session,
        phone_number=normalized,
        phone_hash_pepper=settings.phone_hash_pepper,
        phone_encryption_key=settings.phone_encryption_key,
    )

    code = generate_otp_code(settings.otp_length)
    otp_row = OtpVerification(
        user_id=user.id,
        channel="whatsapp",
        purpose="dashboard_link",
        code_hash=hash_otp_code(code, settings.phone_hash_pepper),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.otp_ttl_seconds),
    )
    session.add(otp_row)

    message_body = (
        f"Your SathyaScan verification code is {code}. "
        f"It expires in {settings.otp_ttl_seconds // 60} minutes. Do not share this code with anyone."
    )
    try:
        await sender.send_text_message(to_phone=normalized, phone_hash=phone_hash, body=message_body)
    except WhatsAppSendError:
        log_event(logger, logging.ERROR, "otp delivery failed", phone_hash=phone_hash[:8])
        await write_audit_log(
            session,
            actor_type="system",
            action="otp_send_failed",
            entity_type="user",
            entity_id=user.id,
            metadata={"channel": "whatsapp"},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not send the verification code via WhatsApp. Please try again shortly.",
        )

    await write_audit_log(
        session,
        actor_type="user",
        action="otp_requested",
        entity_type="user",
        entity_id=user.id,
        metadata={"channel": "whatsapp", "purpose": "dashboard_link"},
    )
    await session.commit()

    return OtpStartResponse(
        message="A verification code has been sent via WhatsApp.",
        expires_in_seconds=settings.otp_ttl_seconds,
    )


@router.post("/auth/link/verify", response_model=TokenResponse)
async def verify_otp_link(
    payload: OtpVerifyRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    normalized = normalize_phone_number(payload.phone_number)
    if normalized is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_GENERIC_OTP_ERROR)

    phone_hash = hash_phone_number(normalized, settings.phone_hash_pepper)
    user_result = await session.execute(select(User).where(User.phone_number_hash == phone_hash))
    user = user_result.scalar_one_or_none()
    if user is None:
        # No user row exists for this phone number at all — i.e. /start was
        # never called for it. Same generic error as every other failure
        # below (see _GENERIC_OTP_ERROR's docstring); not audited (no
        # user_id to attach the entry to, and phone/phone_hash may never
        # appear in audit metadata — app/agent/audit.py enforces this).
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_GENERIC_OTP_ERROR)

    # Expiry and attempts-remaining are filtered IN THE QUERY, not compared
    # in Python after loading — a naive datetime read back from SQLite
    # (aiosqlite drops tzinfo on round-trip, unlike a real Postgres
    # TIMESTAMPTZ) would otherwise raise `TypeError: can't compare
    # offset-naive and offset-aware datetimes` against
    # datetime.now(timezone.utc). Same established pattern as
    # app/agent/duplicate_detection.py's find_reusable_analysis (compares
    # created_at >= cutoff at the SQL level, never in Python).
    now = datetime.now(timezone.utc)
    otp_result = await session.execute(
        select(OtpVerification)
        .where(
            OtpVerification.user_id == user.id,
            OtpVerification.purpose == "dashboard_link",
            OtpVerification.consumed_at.is_(None),
            OtpVerification.expires_at >= now,
            OtpVerification.attempts < settings.otp_max_attempts,
        )
        .order_by(OtpVerification.created_at.desc())
        .limit(1)
    )
    otp_row = otp_result.scalar_one_or_none()

    if otp_row is None:
        await write_audit_log(
            session,
            actor_type="user",
            action="otp_verification_failed",
            entity_type="user",
            entity_id=user.id,
            metadata={"reason": "no_active_code"},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_GENERIC_OTP_ERROR)

    otp_row.attempts += 1  # counts THIS attempt regardless of outcome — bounds brute force

    submitted_hash = hash_otp_code(payload.otp_code, settings.phone_hash_pepper)
    if not hmac.compare_digest(submitted_hash, otp_row.code_hash):
        await write_audit_log(
            session,
            actor_type="user",
            action="otp_verification_failed",
            entity_type="user",
            entity_id=user.id,
            metadata={"reason": "incorrect_code", "attempts": otp_row.attempts},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_GENERIC_OTP_ERROR)

    # Correct code: consume it (single-use — replay of the same code, even
    # within its validity window, is rejected by the `consumed_at.is_(None)`
    # filter above on any subsequent attempt).
    otp_row.consumed_at = now

    dashboard_account_result = await session.execute(
        select(DashboardAccount).where(DashboardAccount.user_id == user.id)
    )
    dashboard_account = dashboard_account_result.scalar_one_or_none()
    if dashboard_account is None:
        dashboard_account = DashboardAccount(user_id=user.id)
        session.add(dashboard_account)

    token = create_access_token(user.id, settings.jwt_secret, settings.jwt_access_token_ttl_seconds)

    await write_audit_log(
        session,
        actor_type="user",
        action="otp_verified",
        entity_type="user",
        entity_id=user.id,
        metadata={"purpose": "dashboard_link"},
    )
    await session.commit()

    return TokenResponse(access_token=token, expires_in_seconds=settings.jwt_access_token_ttl_seconds)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_token(
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
    current: TokenPayload = Depends(get_current_token_payload),
) -> TokenResponse:
    """Sliding-session refresh (app/core/jwt_auth.py's module docstring):
    requires the CURRENT token to still be valid (unexpired, unrevoked —
    enforced by get_current_token_payload, the same dependency every other
    protected route uses) and rotates it — the old token is revoked
    immediately after the new one is issued, so a refreshed-away token can't
    be replayed even if it had leaked."""
    new_token = create_access_token(current.user_id, settings.jwt_secret, settings.jwt_access_token_ttl_seconds)
    await revoke_token(redis, current)

    await write_audit_log(
        session,
        actor_type="user",
        action="token_refreshed",
        entity_type="user",
        entity_id=current.user_id,
    )
    await session.commit()

    return TokenResponse(access_token=new_token, expires_in_seconds=settings.jwt_access_token_ttl_seconds)


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    redis: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
    current: TokenPayload = Depends(get_current_token_payload),
) -> LogoutResponse:
    await revoke_token(redis, current)
    await write_audit_log(
        session,
        actor_type="user",
        action="logout",
        entity_type="user",
        entity_id=current.user_id,
    )
    await session.commit()
    return LogoutResponse()


@router.get("/me", response_model=MeResponse)
async def get_me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        user_id=user.id,
        preferred_language=user.preferred_language,
        auto_detect_language=user.auto_detect_language,
        privacy_mode=user.privacy_mode,
        created_at=user.created_at,
        last_active_at=user.last_active_at,
    )
