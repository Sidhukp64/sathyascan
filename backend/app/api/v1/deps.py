"""
FastAPI dependencies for Phase 6 dashboard/auth routes (app/api/v1/routers/*).

get_redis/get_db_sessionmaker are imported from
app.webhook.whatsapp.router — NOT redefined here — deliberately, not just for
DRY: tests/conftest.py's `client` fixture overrides FastAPI dependencies by
function-object identity (`app.dependency_overrides[get_db_sessionmaker] =
...`). A second, separately-defined `def get_db_sessionmaker(...)` with
identical body would be a DIFFERENT object and silently NOT receive that
override — dashboard routes would fall through to the real (uncredentialed,
non-existent-in-this-sandbox) Postgres/Redis instead of the test doubles.
Reusing the exact same functions is the only correct option here, not a
style preference.

get_db_session is the first per-REQUEST (not per-background-task) DB session
dependency in this codebase — Phases 1-5 only ever touched Postgres from
BackgroundTasks via app/db/session.py's `session_scope`. Phase 6 reuses that
exact same context manager rather than re-implementing commit/rollback
handling, just entered from a FastAPI dependency instead of a background task.
"""

import logging
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.jwt_auth import (
    ExpiredTokenError,
    InvalidTokenError,
    TokenPayload,
    decode_access_token,
    is_token_revoked,
)
from app.core.logging import log_event
from app.core.rate_limit import RateLimiter
from app.db.session import session_scope
from app.models.admin_user import AdminUser
from app.models.user import User
from app.webhook.whatsapp.router import get_db_sessionmaker, get_redis

logger = logging.getLogger(__name__)

__all__ = [
    "get_redis",
    "get_db_sessionmaker",
    "get_db_session",
    "get_current_token_payload",
    "get_current_user_id",
    "get_current_user",
    "get_current_admin_token_payload",
    "get_current_admin",
    "require_admin_role",
    "require_moderator_or_admin_role",
]

# auto_error=False so a missing/malformed Authorization header falls through
# to our own check below and gets OUR consistent {"detail": ...} 401 body,
# not fastapi.security's default one.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_db_session(
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_db_sessionmaker),
):
    async with session_scope(sessionmaker) as session:
        yield session


async def get_current_token_payload(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> TokenPayload:
    """Validates a dashboard JWT end-to-end: presence -> signature/expiry ->
    revocation.

    Redis-unavailable posture (revocation-check failure) is FAIL CLOSED —
    the same posture app/agent/safety_gate.py already documents for its own
    provider-error case ("ERROR is treated as do-not-proceed — fail
    closed"), extended here from content-safety to auth. Silently degrading
    to "assume not revoked" on a Redis outage would let a deliberately
    logged-out/revoked token keep working, which is a worse failure mode
    than a temporarily-503ing dashboard — this is a Phase 6 design decision,
    not something the locked docs specify, made by direct analogy to the one
    fail-open/fail-closed precedent this codebase already sets.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token.")

    token = credentials.credentials
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except ExpiredTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired.")
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is invalid.")

    try:
        revoked = await is_token_revoked(redis, payload.jti)
    except Exception as exc:  # noqa: BLE001 - a Redis outage must fail closed, not crash unhandled
        log_event(logger, logging.ERROR, "revocation check unavailable", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable. Please try again shortly.",
        )

    if revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked.")

    # Phase 9 — structural token-type separation (app/core/jwt_auth.py): an
    # admin token must never work against a regular dashboard route, even
    # in the pathological case of a colliding user_id/admin_id UUID. Same
    # generic 401 body as every other failure above — a distinct error
    # message here would leak "this token IS validly-signed, just the wrong
    # kind," which is more information than a rejected request should give.
    if payload.token_type != "dashboard":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is invalid.")

    # Phase 7 — per-user rate limiting (decisions.md §12/§15), applied here so
    # EVERY authenticated dashboard route inherits it automatically (every
    # protected route depends on get_current_user_id -> this function, or
    # this function directly for /auth/refresh and /auth/logout) rather than
    # each route remembering to add its own Depends(). Reuses
    # app.core.rate_limit.RateLimiter unchanged — same fixed-window
    # mechanism Phase 1 built for the webhook, keyed by user_id instead of
    # phone_hash. Deliberately checked only AFTER signature/expiry/revocation
    # all pass — a garbage/expired/revoked token is already rejected above
    # and never consumes a legitimate user's rate-limit budget.
    limiter = RateLimiter(redis, settings.dashboard_rate_limit_per_user_per_minute)
    rate_result = await limiter.check_and_increment(f"user:{payload.user_id}")
    if not rate_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down and try again shortly.",
        )

    return payload


async def get_current_user_id(payload: TokenPayload = Depends(get_current_token_payload)) -> uuid.UUID:
    return payload.user_id


async def get_current_user(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Loads the full row so route handlers can read preferred_language/
    privacy_mode/etc. directly. A still-valid, unexpired, unrevoked token can
    outlive its account (e.g. a future account-deletion flow) — checking
    is_deleted here, not just trusting the token's identity claim, is why
    this dependency exists on top of get_current_user_id rather than routes
    using the bare user_id everywhere."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or user.is_deleted:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found.")
    # Phase 9 — admin "Suspend user" (roadmap §9.1). Same "check the live
    # row, not just the token's identity claim" reasoning is_deleted above
    # already established — a still-valid, unexpired token issued BEFORE a
    # suspension must stop working on its very next authenticated request,
    # not just at its next login.
    if user.is_suspended:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been suspended.")
    return user


async def get_current_admin_token_payload(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> TokenPayload:
    """Admin-token counterpart to get_current_token_payload — same
    signature/expiry/revocation validation, but requires `token_type ==
    "admin"` instead of rejecting it, and rate-limits per admin_id using a
    separate, admin-specific limit (decisions.md-style per-actor rate
    limiting, extended here from users to admins) rather than reusing the
    dashboard counter/config value, so a burst of admin API traffic can
    never be confused with (or throttled by) a regular user's budget."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token.")

    token = credentials.credentials
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except ExpiredTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired.")
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is invalid.")

    try:
        revoked = await is_token_revoked(redis, payload.jti)
    except Exception as exc:  # noqa: BLE001 - fail closed, same posture as the dashboard path
        log_event(logger, logging.ERROR, "admin revocation check unavailable", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable. Please try again shortly.",
        )

    if revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked.")

    # Structural separation, mirrored from get_current_token_payload: a
    # DASHBOARD token must never work against an admin route either.
    if payload.token_type != "admin":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is invalid.")

    limiter = RateLimiter(redis, settings.admin_rate_limit_per_admin_per_minute)
    rate_result = await limiter.check_and_increment(f"admin:{payload.user_id}")
    if not rate_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down and try again shortly.",
        )

    return payload


async def get_current_admin(
    payload: TokenPayload = Depends(get_current_admin_token_payload),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUser:
    """Loads the live admin_users row — same "don't just trust the token's
    identity claim" reasoning as get_current_user: a still-valid token for
    an admin whose access was just deactivated must stop working
    immediately, not at its next login."""
    result = await session.execute(select(AdminUser).where(AdminUser.id == payload.user_id))
    admin = result.scalar_one_or_none()
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin account not found.")
    return admin


async def require_admin_role(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    """Strict RBAC gate (roadmap §9.1): only role == "admin" — NOT
    "moderator" — may reach routes behind this dependency (user
    suspension/reactivation, system health/jobs/providers, the raw
    audit-log viewer). A moderator token that is otherwise perfectly valid
    still gets 403 here, never silently allowed through."""
    if admin.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This action requires the admin role.")
    return admin


async def require_moderator_or_admin_role(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
    """Wider RBAC gate for appeals/moderation review — both roles may
    triage reports and appeals; only "admin" may touch user accounts or
    system internals (see require_admin_role)."""
    if admin.role not in ("admin", "moderator"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient admin privileges.")
    return admin
