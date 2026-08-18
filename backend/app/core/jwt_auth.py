"""
Dashboard JWT auth (Phase 6, api-design.md's "WhatsApp-OTP-based linking" §
"issues a JWT"). Access-token-only design (no separate refresh-token
secret/table) — `refresh` re-validates the current token and, if still
valid, issues a new one with a fresh expiry (sliding session), avoiding a
second credential type to secure/rotate/revoke. `logout` is a real,
server-enforced revocation, not just "the client discards the token" —
implemented via Redis (reusing the SAME Redis client every other Phase 1-5
rate-limit/idempotency/concurrency mechanism already uses, not a new piece
of infrastructure), storing revoked token IDs (`jti`) with a TTL equal to
the token's own remaining lifetime, so the revocation set never grows
unboundedly.

Never logs a token, its claims, or the JWT secret — only the `jti` (a random
UUID with no sensitive content of its own) and `user_id`, matching
decisions.md §7/§9's "never log sensitive content" rule extended from phone
numbers to auth material.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from redis.asyncio import Redis

from app.core.logging import log_event

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_REVOCATION_KEY_PREFIX = "jwt:revoked:"


class JWTAuthError(Exception):
    """Base class — maps to 401 Unauthorized at the API layer."""


class InvalidTokenError(JWTAuthError):
    pass


class ExpiredTokenError(JWTAuthError):
    pass


class RevokedTokenError(JWTAuthError):
    pass


@dataclass(frozen=True)
class TokenPayload:
    user_id: uuid.UUID
    jti: str
    issued_at: datetime
    expires_at: datetime
    # Phase 9 — admin/dashboard token separation (roadmap §9.1). Default
    # "dashboard" so every token issued before this field existed (and
    # every test/call site that doesn't pass it) keeps its exact original
    # meaning — this is additive, not a breaking change to the token shape.
    # "admin" tokens are issued only by app/api/v1/routers/admin_auth.py,
    # for an app/models/admin_user.py row's id, never a regular user's.
    # Checked explicitly (not just structurally) in TWO places — see
    # app/api/v1/deps.py's get_current_token_payload (rejects "admin") and
    # get_current_admin_token_payload (rejects anything but "admin") — so a
    # token minted for one purpose can never be replayed against the other,
    # even in the pathological case of a colliding user_id/admin_id UUID.
    token_type: str = "dashboard"


def create_access_token(
    user_id: uuid.UUID, secret: str, ttl_seconds: int, *, token_type: str = "dashboard"
) -> str:
    if not secret:
        # Fail loud, same discipline as hash_phone_number's empty-pepper
        # guard — issuing a token signed with an empty secret would be
        # trivially forgeable.
        raise ValueError("JWT_SECRET is not configured — refusing to issue a token with an empty secret.")

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
        "typ": token_type,
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, secret: str) -> TokenPayload:
    """Validates signature and expiry only — does NOT check revocation
    (that needs Redis, an async call); callers must separately await
    `is_token_revoked` — see get_current_user_id in app/api/v1/deps.py."""
    if not secret:
        raise InvalidTokenError("JWT_SECRET is not configured")

    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredTokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers malformed tokens, bad signature, wrong algorithm, etc. —
        # PyJWT's own exception hierarchy, never re-raised with exc's own
        # message verbatim to the caller (it may echo back attacker-supplied
        # token fragments); the caller only sees our own generic message.
        raise InvalidTokenError("token is invalid") from exc

    try:
        user_id = uuid.UUID(claims["sub"])
        jti = claims["jti"]
        issued_at = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)
        expires_at = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        # "typ" didn't exist on tokens issued before Phase 9 — .get() with
        # the same "dashboard" default as the dataclass field itself, so an
        # old (still-valid, unexpired) token decodes to exactly the meaning
        # it always had, never InvalidTokenError just because Phase 9 shipped.
        token_type = claims.get("typ", "dashboard")
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError("token payload is malformed") from exc

    return TokenPayload(
        user_id=user_id, jti=jti, issued_at=issued_at, expires_at=expires_at, token_type=token_type
    )


async def revoke_token(redis: Redis, payload: TokenPayload) -> None:
    """Called on logout. TTL = the token's own remaining lifetime — a
    revoked-but-already-expired entry is pointless to keep, so this never
    accumulates unboundedly regardless of how many users log out."""
    remaining_seconds = int((payload.expires_at - datetime.now(timezone.utc)).total_seconds())
    if remaining_seconds <= 0:
        return  # already expired — nothing to revoke
    await redis.set(f"{_REVOCATION_KEY_PREFIX}{payload.jti}", "1", ex=remaining_seconds)
    log_event(logger, logging.INFO, "JWT revoked (logout)", jti=payload.jti, user_id=str(payload.user_id))


async def is_token_revoked(redis: Redis, jti: str) -> bool:
    return bool(await redis.exists(f"{_REVOCATION_KEY_PREFIX}{jti}"))
