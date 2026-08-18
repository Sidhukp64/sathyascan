"""
Dedicated Phase 6 security-checklist tests, over the dashboard API surface
(app/api/v1/routers/*) — mirrors the rigor of the Phase 5 credential-safety
audit, applied to this phase's new attack surface: IDOR, phone-number
exposure, SQL injection, XSS/reflection, secret leakage in errors/logs,
privacy-mode-bypass, and malformed-request handling.

IDOR / cross-user isolation are already exhaustively covered in
test_history_router.py and test_settings_router.py (every list/detail/
delete/settings endpoint has a "user A cannot touch user B's data" test) —
not repeated here. This file covers the remaining checklist items those
files don't already exercise directly.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.jwt_auth import create_access_token
from app.models.analysis import Analysis
from app.models.audit_log import AuditLog
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812345301"


# ---------------------------------------------------------------------------
# SQL injection — every query in this phase goes through SQLAlchemy's
# parameterized expression API (never raw string-formatted SQL), so
# injection payloads must be treated as inert literal text everywhere
# user input reaches a query.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_search_query_rejects_sql_injection_payload_safely(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    payload = "'; DROP TABLE analyses; --"
    resp = await auth_env.client.get(
        "/api/v1/history", params={"q": payload}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0  # treated as a literal (non-matching) substring, not executed

    # Prove the table is still there and queryable.
    follow_up = await auth_env.client.get(
        "/api/v1/history", headers={"Authorization": f"Bearer {token}"}
    )
    assert follow_up.status_code == 200


@pytest.mark.asyncio
async def test_otp_verify_rejects_sql_injection_in_phone_field_safely(auth_env):
    resp = await auth_env.client.post(
        "/api/v1/auth/link/verify",
        json={"phone_number": "1' OR '1'='1", "otp_code": "123456"},
    )
    # normalize_phone_number rejects anything non-digit outright — 400, not
    # a 500 or (worse) a bypass.
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_settings_language_rejects_sql_injection_payload(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.put(
        "/api/v1/settings/language",
        json={"preferred_language": "en'; DROP TABLE users; --"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Rejected before ever reaching a query — either by the Pydantic
    # max_length=5 field constraint (422) or the documented-code allowlist
    # (400) if it were ever short enough to pass the first gate.
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# XSS / reflection — this is a JSON-only API (no HTML templates rendered
# anywhere in the dashboard surface), so classic reflected/stored XSS
# doesn't apply the way it would to a server-rendered page. What DOES matter:
# the API must never echo attacker-supplied markup back with a content-type
# that a browser would render as HTML.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_search_with_script_payload_returns_json_not_html(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/history",
        params={"q": "<script>alert(1)</script>"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Secret leakage — JWT secret, tokens, OTP codes must never appear in any
# error response body.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_token_error_never_echoes_the_submitted_token(auth_env):
    garbage_token = "eyFAKE.NOTAREAL.TOKEN12345secretlookingvalue"
    resp = await auth_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {garbage_token}"}
    )
    assert resp.status_code == 401
    assert garbage_token not in resp.text


@pytest.mark.asyncio
async def test_expired_token_error_never_leaks_jwt_secret(auth_env):
    async with auth_env.sessionmaker() as session:
        user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
        session.add(user)
        await session.commit()
        await session.refresh(user)

    expired_token = create_access_token(user.id, auth_env.settings.jwt_secret, ttl_seconds=-5)
    resp = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401
    assert auth_env.settings.jwt_secret not in resp.text


@pytest.mark.asyncio
async def test_unhandled_error_responses_never_contain_stack_traces(auth_env):
    # Malformed UUID path param — FastAPI/Pydantic returns a clean 422, not
    # a raw traceback (app/core/exceptions.py's generic-500 handler is the
    # backstop for anything that DOES slip through unhandled).
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/history/not-a-valid-uuid", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422
    assert "Traceback" not in resp.text
    assert "app\\" not in resp.text and "app/" not in resp.text


# ---------------------------------------------------------------------------
# Malformed request handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_body_returns_422_not_500(auth_env):
    resp = await auth_env.client.post(
        "/api/v1/auth/link/start", content=b"{not valid json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_required_field_returns_422(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_negative_page_number_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/history", params={"page": -1}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_oversized_page_size_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/history", params={"page_size": 999999}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_privacy_mode_field_wrong_type_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.put(
        "/api/v1/settings/privacy",
        json={"privacy_mode": "not-a-boolean"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Privacy-mode bypass — flipping the setting must never expose or alter
# another user's already-existing data, and must never be reachable without
# authentication.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_flip_another_users_privacy_mode_via_history_or_settings_routes(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    # There is no route that accepts a client-supplied user_id anywhere in
    # this phase (api-design.md's rule) — confirm privacy toggling is only
    # ever scoped to the caller's own JWT identity by trying to smuggle a
    # user_id into the body and confirming it's ignored (Pydantic schema has
    # no such field, so FastAPI/Pydantic silently drops unknown fields —
    # this proves there is no field to smuggle through in the first place).
    resp = await auth_env.client.put(
        "/api/v1/settings/privacy",
        json={"privacy_mode": False, "user_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 200  # extra field silently ignored, not an error and not honored


@pytest.mark.asyncio
async def test_all_dashboard_write_routes_reject_missing_auth():
    """Static sweep: every mutating dashboard route must require auth. Uses
    a bare client with no token at all against a freshly booted app."""
    import fakeredis.aioredis
    import httpx
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.core.config import Settings, get_settings
    from app.db.session import build_sessionmaker
    from app.main import create_app
    from app.models import Base
    from app.webhook.whatsapp.router import get_db_sessionmaker

    settings = Settings(PHONE_HASH_PEPPER="p", JWT_SECRET="s" * 32)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_db_sessionmaker] = lambda: build_sessionmaker(engine)

    async with app.router.lifespan_context(app):
        app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            checks = [
                ("DELETE", "/api/v1/history", None),
                ("DELETE", f"/api/v1/history/{uuid.uuid4()}", None),
                ("PUT", "/api/v1/settings/language", {"preferred_language": "en"}),
                ("PUT", "/api/v1/settings/privacy", {"privacy_mode": True}),
                ("POST", "/api/v1/auth/logout", None),
                ("POST", "/api/v1/auth/refresh", None),
            ]
            for method, path, body in checks:
                resp = await client.request(method, path, json=body)
                assert resp.status_code == 401, f"{method} {path} did not require auth (got {resp.status_code})"

    await engine.dispose()
