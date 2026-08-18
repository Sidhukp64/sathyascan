"""
Integration tests for Phase 9 admin Moderation review
(app/api/v1/routers/admin_moderation.py) — RBAC, state transitions.
"""

import uuid

import pytest

from tests.conftest import create_admin_and_get_token, login_and_get_token

PHONE_A = "919812380001"
ADMIN_EMAIL = "admin3@example.com"
MODERATOR_EMAIL = "mod3@example.com"
PASSWORD = "correct-horse-battery-staple"


async def _seed_report(auth_env, phone: str) -> str:
    token = await login_and_get_token(auth_env, phone)
    resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={
            "report_type": "spam",
            "target_type": "url",
            "target_url": "https://example.com/suspicious",
            "description": "looks like spam",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_admin_moderation_requires_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/admin/moderation/reports")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_normal_user_cannot_access_admin_moderation(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/admin/moderation/reports", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_moderator_can_list_and_review_reports(auth_env):
    report_id = await _seed_report(auth_env, PHONE_A)
    mod_token = await create_admin_and_get_token(auth_env, MODERATOR_EMAIL, PASSWORD, role="moderator")

    list_resp = await auth_env.client.get(
        "/api/v1/admin/moderation/reports", headers={"Authorization": f"Bearer {mod_token}"}
    )
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    review_resp = await auth_env.client.post(
        f"/api/v1/admin/moderation/reports/{report_id}/review",
        json={"status": "reviewed", "admin_notes": "confirmed spam"},
        headers={"Authorization": f"Bearer {mod_token}"},
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["status"] == "reviewed"


@pytest.mark.asyncio
async def test_valid_transition_open_to_actioned(auth_env):
    report_id = await _seed_report(auth_env, PHONE_A)
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    resp = await auth_env.client.post(
        f"/api/v1/admin/moderation/reports/{report_id}/review",
        json={"status": "actioned"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "actioned"
    assert body["resolved_at"] is not None


@pytest.mark.asyncio
async def test_invalid_transition_from_terminal_state_rejected(auth_env):
    report_id = await _seed_report(auth_env, PHONE_A)
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    first = await auth_env.client.post(
        f"/api/v1/admin/moderation/reports/{report_id}/review",
        json={"status": "dismissed"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert first.status_code == 200

    second = await auth_env.client.post(
        f"/api/v1/admin/moderation/reports/{report_id}/review",
        json={"status": "actioned"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_review_nonexistent_report_404s(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    resp = await auth_env.client.post(
        f"/api/v1/admin/moderation/reports/{uuid.uuid4()}/review",
        json={"status": "dismissed"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404
