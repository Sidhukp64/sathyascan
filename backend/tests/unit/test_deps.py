"""
Unit tests for app/api/v1/deps.py's auth dependencies — called directly as
plain async functions (FastAPI itself resolves each Depends() argument
before invoking the function body, so testing the body directly with
hand-built arguments exercises the exact same logic without needing a full
HTTP round trip; tests/integration covers the end-to-end router behavior).
"""

import uuid
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.deps import (
    get_current_token_payload,
    get_current_user,
    get_current_user_id,
)
from app.core.config import Settings
from app.core.jwt_auth import TokenPayload, create_access_token
from app.db.base import Base
from app.db.session import build_sessionmaker
from app.models.user import User

SECRET = "test-jwt-secret-for-deps"


def _settings(**overrides) -> Settings:
    return Settings(JWT_SECRET=SECRET, PHONE_HASH_PEPPER="test-pepper", **overrides)


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class _RaisingRedis:
    """Stands in for a Redis client whose connection is down — every call
    raises, same as redis.asyncio would on a real connection failure."""

    async def exists(self, *_args, **_kwargs):
        raise ConnectionError("redis unavailable (simulated)")


@pytest.mark.asyncio
async def test_missing_credentials_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        await get_current_token_payload(credentials=None, settings=_settings(), redis=None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_empty_credentials_string_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        await get_current_token_payload(credentials=_creds(""), settings=_settings(), redis=None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_malformed_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        await get_current_token_payload(
            credentials=_creds("not-a-real-jwt"), settings=_settings(), redis=fakeredis.aioredis.FakeRedis()
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_raises_401():
    token = create_access_token(uuid.uuid4(), SECRET, ttl_seconds=-10)
    with pytest.raises(HTTPException) as exc_info:
        await get_current_token_payload(
            credentials=_creds(token), settings=_settings(), redis=fakeredis.aioredis.FakeRedis()
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_returns_payload():
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=3600)
    payload = await get_current_token_payload(
        credentials=_creds(token), settings=_settings(), redis=fakeredis.aioredis.FakeRedis()
    )
    assert payload.user_id == user_id


@pytest.mark.asyncio
async def test_revoked_token_raises_401():
    redis = fakeredis.aioredis.FakeRedis()
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=3600)
    # Manually mark this jti revoked, same key shape jwt_auth.revoke_token uses.
    from app.core.jwt_auth import decode_access_token

    payload = decode_access_token(token, SECRET)
    await redis.set(f"jwt:revoked:{payload.jti}", "1", ex=3600)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_token_payload(credentials=_creds(token), settings=_settings(), redis=redis)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_redis_outage_during_revocation_check_fails_closed_with_503():
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SECRET, ttl_seconds=3600)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_token_payload(credentials=_creds(token), settings=_settings(), redis=_RaisingRedis())
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_get_current_user_id_extracts_user_id_from_payload():
    user_id = uuid.uuid4()
    payload = TokenPayload(
        user_id=user_id,
        jti=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert await get_current_user_id(payload=payload) == user_id


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


async def _make_user(session: AsyncSession, **overrides) -> User:
    user = User(
        phone_number_encrypted=b"ciphertext",
        phone_number_hash=overrides.pop("phone_number_hash", uuid.uuid4().hex),
        **overrides,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_get_current_user_returns_active_user(db_session: AsyncSession):
    user = await _make_user(db_session)
    loaded = await get_current_user(user_id=user.id, session=db_session)
    assert loaded.id == user.id


@pytest.mark.asyncio
async def test_get_current_user_rejects_soft_deleted_user(db_session: AsyncSession):
    user = await _make_user(db_session, is_deleted=True)
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(user_id=user.id, session=db_session)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_rejects_nonexistent_user(db_session: AsyncSession):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(user_id=uuid.uuid4(), session=db_session)
    assert exc_info.value.status_code == 401
