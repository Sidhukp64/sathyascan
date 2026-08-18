"""
Integration tests for Phase 9 admin core API (app/api/v1/routers/admin.py)
— user management, RBAC boundaries, stats, system health/jobs/providers,
audit-log viewing.
"""

import pytest

from tests.conftest import create_admin_and_get_token, login_and_get_token

ADMIN_EMAIL = "admin@example.com"
MODERATOR_EMAIL = "mod@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.mark.asyncio
async def test_admin_routes_require_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/admin/users")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_normal_dashboard_user_cannot_access_admin_users(auth_env):
    """Strict RBAC — a regular authenticated end-user (not an admin_users
    row at all) must be rejected, not just an unauthenticated caller."""
    user_token = await login_and_get_token(auth_env, "919812340101")
    resp = await auth_env.client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_moderator_cannot_access_admin_only_user_management(auth_env):
    """Strict RBAC — role == "moderator" is a valid, authenticated admin
    token, but must still be 403'd on admin-only routes (user
    suspend/reactivate, stats, system internals)."""
    mod_token = await create_admin_and_get_token(auth_env, MODERATOR_EMAIL, PASSWORD, role="moderator")
    resp = await auth_env.client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {mod_token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_cannot_suspend_users(auth_env):
    mod_token = await create_admin_and_get_token(auth_env, MODERATOR_EMAIL, PASSWORD, role="moderator")
    user_token = await login_and_get_token(auth_env, "919812340102")
    # Get the user's own id via /api/v1/me is not built; seed-and-fetch via DB instead.
    from sqlalchemy import select

    from app.models.user import User

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        user_id = result.scalars().one().id

    resp = await auth_env.client.post(
        f"/api/v1/admin/users/{user_id}/suspend",
        json={"reason": "test"},
        headers={"Authorization": f"Bearer {mod_token}"},
    )
    assert resp.status_code == 403


async def _seed_user(auth_env, phone_number: str):
    from sqlalchemy import select

    from app.models.user import User

    await login_and_get_token(auth_env, phone_number)
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one()


@pytest.mark.asyncio
async def test_admin_can_list_and_view_users(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    user = await _seed_user(auth_env, "919812340103")

    list_resp = await auth_env.client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] >= 1

    detail_resp = await auth_env.client.get(
        f"/api/v1/admin/users/{user.id}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert detail_resp.status_code == 200
    body = detail_resp.json()
    assert body["id"] == str(user.id)
    assert "phone_number_hash" not in body
    assert "phone_number_encrypted" not in body


@pytest.mark.asyncio
async def test_admin_user_detail_404_for_unknown_id(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    import uuid

    resp = await auth_env.client.get(
        f"/api/v1/admin/users/{uuid.uuid4()}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_suspend_and_reactivate_user(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    user = await _seed_user(auth_env, "919812340104")

    suspend_resp = await auth_env.client.post(
        f"/api/v1/admin/users/{user.id}/suspend",
        json={"reason": "spam"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert suspend_resp.status_code == 200
    assert suspend_resp.json()["is_suspended"] is True

    # Suspending an already-suspended user is rejected, not silently repeated.
    again_resp = await auth_env.client.post(
        f"/api/v1/admin/users/{user.id}/suspend",
        json={"reason": "spam again"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert again_resp.status_code == 400

    reactivate_resp = await auth_env.client.post(
        f"/api/v1/admin/users/{user.id}/reactivate", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert reactivate_resp.status_code == 200
    assert reactivate_resp.json()["is_suspended"] is False

    # Reactivating a non-suspended user is rejected too.
    again_reactivate_resp = await auth_env.client.post(
        f"/api/v1/admin/users/{user.id}/reactivate", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert again_reactivate_resp.status_code == 400


@pytest.mark.asyncio
async def test_suspended_user_cannot_use_dashboard(auth_env):
    """Suspension is functionally enforced, not just a DB flag — a
    still-valid dashboard token stops working the instant is_suspended
    flips (app/api/v1/deps.py::get_current_user)."""
    from sqlalchemy import select

    from app.models.user import User

    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    user_token = await login_and_get_token(auth_env, "919812340105")
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        user = result.scalars().one()

    await auth_env.client.post(
        f"/api/v1/admin/users/{user.id}/suspend",
        json={"reason": "test"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    resp = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_stats_users_and_analyses(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    await _seed_user(auth_env, "919812340106")

    user_stats_resp = await auth_env.client.get(
        "/api/v1/admin/stats/users", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert user_stats_resp.status_code == 200
    assert user_stats_resp.json()["total_users"] >= 1

    analysis_stats_resp = await auth_env.client.get(
        "/api/v1/admin/stats/analyses", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert analysis_stats_resp.status_code == 200
    assert "by_input_type" in analysis_stats_resp.json()


@pytest.mark.asyncio
async def test_admin_system_health_jobs_providers(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    health_resp = await auth_env.client.get(
        "/api/v1/admin/system/health", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert health_resp.status_code == 200
    # Same honest precedent as /health's own test (test_webhook.py):
    # app.state.db_engine points at the REAL (unreachable-in-this-sandbox)
    # DATABASE_URL, not the test's overridden sessionmaker — "degraded" is
    # the correct, honest status here, not a bug.
    assert health_resp.json()["status"] in {"ok", "degraded"}

    jobs_resp = await auth_env.client.get(
        "/api/v1/admin/system/jobs", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert jobs_resp.status_code == 200
    assert "jobs" in jobs_resp.json()

    providers_resp = await auth_env.client.get(
        "/api/v1/admin/system/providers", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert providers_resp.status_code == 200
    body = providers_resp.json()
    assert body["speech_to_text_provider"] in ("null", "real")


@pytest.mark.asyncio
async def test_moderator_can_view_system_status_read_only_is_still_admin_only(auth_env):
    """system/* is admin-only, not admin-or-moderator (see
    app/api/v1/routers/admin.py's module docstring) — a moderator gets 403."""
    mod_token = await create_admin_and_get_token(auth_env, MODERATOR_EMAIL, PASSWORD, role="moderator")
    resp = await auth_env.client.get(
        "/api/v1/admin/system/health", headers={"Authorization": f"Bearer {mod_token}"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_audit_log_protected_from_ordinary_users_and_moderators(auth_env):
    """Roadmap §9.4: 'Protect audit logs from ordinary users.'"""
    user_token = await login_and_get_token(auth_env, "919812340107")
    mod_token = await create_admin_and_get_token(auth_env, MODERATOR_EMAIL, PASSWORD, role="moderator")

    user_resp = await auth_env.client.get("/api/v1/admin/audit-log", headers={"Authorization": f"Bearer {user_token}"})
    assert user_resp.status_code == 401

    mod_resp = await auth_env.client.get("/api/v1/admin/audit-log", headers={"Authorization": f"Bearer {mod_token}"})
    assert mod_resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_view_audit_log_including_own_login(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    resp = await auth_env.client.get("/api/v1/admin/audit-log", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(item["action"] == "admin_login" for item in body["items"])
