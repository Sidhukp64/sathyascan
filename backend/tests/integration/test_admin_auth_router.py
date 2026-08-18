"""
Integration tests for Phase 9 admin auth (app/api/v1/routers/admin_auth.py)
— login/logout, per-IP rate limiting, generic-error enumeration resistance,
inactive-account rejection, and structural token-type separation from
dashboard tokens.
"""

import pytest

from app.core.admin_security import hash_password, normalize_admin_email
from app.models.admin_user import AdminUser
from tests.conftest import create_admin_and_get_token, login_and_get_token

ADMIN_EMAIL = "ops@example.com"
ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.mark.asyncio
async def test_admin_login_succeeds_with_correct_credentials(auth_env):
    token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert token


@pytest.mark.asyncio
async def test_admin_login_rejects_wrong_password(auth_env):
    await create_admin_and_get_token(auth_env, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await auth_env.client.post(
        "/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_login_rejects_unknown_email_with_same_generic_error(auth_env):
    """Enumeration resistance: unknown email and wrong password return the
    exact same status/detail, per the module's own docstring."""
    resp_unknown = await auth_env.client.post(
        "/api/v1/admin/auth/login", json={"email": "nobody@example.com", "password": "whatever"}
    )
    await create_admin_and_get_token(auth_env, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp_wrong_password = await auth_env.client.post(
        "/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"}
    )
    assert resp_unknown.status_code == resp_wrong_password.status_code == 401
    assert resp_unknown.json()["detail"] == resp_wrong_password.json()["detail"]


@pytest.mark.asyncio
async def test_admin_login_rejects_inactive_account(auth_env):
    async with auth_env.sessionmaker() as session:
        admin = AdminUser(
            email=normalize_admin_email(ADMIN_EMAIL),
            password_hash=hash_password(ADMIN_PASSWORD),
            role="admin",
            is_active=False,
        )
        session.add(admin)
        await session.commit()

    resp = await auth_env.client.post("/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_login_email_is_case_insensitive(auth_env):
    await create_admin_and_get_token(auth_env, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await auth_env.client.post(
        "/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL.upper(), "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_login_rate_limited_per_ip(auth_env):
    limit = auth_env.settings.admin_login_rate_limit_per_ip_per_minute
    for _ in range(limit):
        await auth_env.client.post("/api/v1/admin/auth/login", json={"email": "x@example.com", "password": "x"})
    resp = await auth_env.client.post("/api/v1/admin/auth/login", json={"email": "x@example.com", "password": "x"})
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_admin_logout_revokes_token(auth_env):
    token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, ADMIN_PASSWORD)
    logout_resp = await auth_env.client.post(
        "/api/v1/admin/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout_resp.status_code == 200

    reuse_resp = await auth_env.client.get(
        "/api/v1/admin/stats/users", headers={"Authorization": f"Bearer {token}"}
    )
    assert reuse_resp.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_token_cannot_be_used_against_admin_routes(auth_env):
    """Structural token-type separation (app/core/jwt_auth.py) — a
    perfectly valid DASHBOARD token must never work on an admin route."""
    dashboard_token = await login_and_get_token(auth_env, "919812340001")
    resp = await auth_env.client.get(
        "/api/v1/admin/stats/users", headers={"Authorization": f"Bearer {dashboard_token}"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_token_cannot_be_used_against_dashboard_routes(auth_env):
    """The reverse of the above — an admin token must never work on a
    regular dashboard route."""
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 401
