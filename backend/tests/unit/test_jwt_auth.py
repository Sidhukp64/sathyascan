"""Unit tests for app/core/jwt_auth.py — issuance, verification, and
Redis-backed revocation. Uses fakeredis.aioredis.FakeRedis, the same
in-memory-fake pattern tests/conftest.py already uses for the webhook's
Redis-backed idempotency/rate-limiting, not a new test-infra pattern."""

import time
import uuid

import fakeredis.aioredis
import jwt as pyjwt
import pytest

from app.core.jwt_auth import (
    ExpiredTokenError,
    InvalidTokenError,
    TokenPayload,
    create_access_token,
    decode_access_token,
    is_token_revoked,
    revoke_token,
)

SECRET = "test-jwt-secret-do-not-use-in-prod"


def test_round_trip_issue_and_decode():
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=3600)

    payload = decode_access_token(token, SECRET)

    assert payload.user_id == user_id
    assert isinstance(payload.jti, str) and payload.jti  # a real, non-empty jti
    assert payload.expires_at > payload.issued_at


def test_each_token_gets_a_unique_jti():
    user_id = uuid.uuid4()
    token_a = create_access_token(user_id, SECRET, ttl_seconds=3600)
    token_b = create_access_token(user_id, SECRET, ttl_seconds=3600)

    assert decode_access_token(token_a, SECRET).jti != decode_access_token(token_b, SECRET).jti


def test_empty_secret_refuses_to_issue():
    with pytest.raises(ValueError):
        create_access_token(uuid.uuid4(), "", ttl_seconds=3600)


def test_expired_token_raises_expired_token_error():
    user_id = uuid.uuid4()
    # ttl in the past — PyJWT's own exp check catches this on decode.
    token = create_access_token(user_id, SECRET, ttl_seconds=-10)

    with pytest.raises(ExpiredTokenError):
        decode_access_token(token, SECRET)


def test_tampered_signature_raises_invalid_token_error():
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=3600)

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, "a-completely-different-secret")


def test_garbage_token_raises_invalid_token_error():
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-real-jwt-at-all", SECRET)


def test_token_missing_required_claims_raises_invalid_token_error():
    # Hand-craft a validly-signed token missing 'jti' entirely — decode_access_token
    # must reject it rather than crash with a raw KeyError.
    bad_token = pyjwt.encode(
        {"sub": str(uuid.uuid4()), "iat": time.time(), "exp": time.time() + 3600},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(bad_token, SECRET)


def test_token_with_non_uuid_subject_raises_invalid_token_error():
    bad_token = pyjwt.encode(
        {"sub": "not-a-uuid", "jti": str(uuid.uuid4()), "iat": time.time(), "exp": time.time() + 3600},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(bad_token, SECRET)


@pytest.mark.asyncio
async def test_unrevoked_token_reports_not_revoked():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await is_token_revoked(redis, str(uuid.uuid4())) is False


@pytest.mark.asyncio
async def test_revoke_then_check_reports_revoked():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=3600)
    payload = decode_access_token(token, SECRET)

    assert await is_token_revoked(redis, payload.jti) is False
    await revoke_token(redis, payload)
    assert await is_token_revoked(redis, payload.jti) is True

    # Revocation must be scoped to this jti only — a different, unrelated
    # token must not be affected.
    other_token = create_access_token(user_id, SECRET, ttl_seconds=3600)
    other_payload = decode_access_token(other_token, SECRET)
    assert await is_token_revoked(redis, other_payload.jti) is False


@pytest.mark.asyncio
async def test_revoking_an_already_expired_token_is_a_safe_noop():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # Build a TokenPayload whose expires_at is already in the past — revoke_token
    # must not crash or set a negative/zero TTL key.
    from datetime import datetime, timedelta, timezone

    payload = TokenPayload(
        user_id=uuid.uuid4(),
        jti=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc) - timedelta(hours=2),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await revoke_token(redis, payload)  # must not raise
    assert await is_token_revoked(redis, payload.jti) is False  # nothing was actually stored


@pytest.mark.asyncio
async def test_revocation_ttl_matches_remaining_token_lifetime():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=100)
    payload = decode_access_token(token, SECRET)

    await revoke_token(redis, payload)
    ttl = await redis.ttl(f"jwt:revoked:{payload.jti}")
    assert 0 < ttl <= 100
