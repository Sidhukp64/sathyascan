"""
Integration tests for Phase 9 admin Appeals review
(app/api/v1/routers/admin_appeals.py) — RBAC, valid/invalid state
transitions, admin visibility across users.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.user import User
from tests.conftest import create_admin_and_get_token, login_and_get_token

PHONE_A = "919812360001"
ADMIN_EMAIL = "admin2@example.com"
MODERATOR_EMAIL = "mod2@example.com"
PASSWORD = "correct-horse-battery-staple"


async def _seed_appeal(auth_env, phone: str) -> tuple[str, str]:
    token = await login_and_get_token(auth_env, phone)
    async with auth_env.sessionmaker() as session:
        user_result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        user_id = user_result.scalars().one().id
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="claim",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result="false",
            language="en",
            privacy_mode_snapshot=True,
            created_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.commit()
        analysis_id = analysis.id

    resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "please review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    return token, resp.json()["id"]


@pytest.mark.asyncio
async def test_admin_appeals_require_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/admin/appeals")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_normal_user_cannot_access_admin_appeals(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get("/api/v1/admin/appeals", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_moderator_can_list_and_review_appeals(auth_env):
    """Moderators ARE allowed on appeals review (see
    app/api/v1/routers/admin_appeals.py's module docstring), unlike
    user-account/system-internal admin routes."""
    _, appeal_id = await _seed_appeal(auth_env, PHONE_A)
    mod_token = await create_admin_and_get_token(auth_env, MODERATOR_EMAIL, PASSWORD, role="moderator")

    list_resp = await auth_env.client.get("/api/v1/admin/appeals", headers={"Authorization": f"Bearer {mod_token}"})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    review_resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "under_review", "admin_notes": "looking into it"},
        headers={"Authorization": f"Bearer {mod_token}"},
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["status"] == "under_review"


@pytest.mark.asyncio
async def test_valid_transition_open_to_approved(auth_env):
    _, appeal_id = await _seed_appeal(auth_env, PHONE_A)
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "approved", "admin_notes": "valid appeal"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["resolved_at"] is not None


@pytest.mark.asyncio
async def test_invalid_transition_from_terminal_state_rejected(auth_env):
    _, appeal_id = await _seed_appeal(auth_env, PHONE_A)
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    first = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "rejected"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert first.status_code == 200

    second = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "approved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_invalid_status_value_rejected(auth_env):
    _, appeal_id = await _seed_appeal(auth_env, PHONE_A)
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "not_a_real_status"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cancelled_appeal_cannot_be_admin_reviewed(auth_env):
    token, appeal_id = await _seed_appeal(auth_env, PHONE_A)
    await auth_env.client.post(f"/api/v1/appeals/{appeal_id}/cancel", headers={"Authorization": f"Bearer {token}"})

    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "approved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_escalated_appeal_can_still_be_approved_or_rejected(auth_env):
    _, appeal_id = await _seed_appeal(auth_env, PHONE_A)
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)

    escalate_resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "escalated"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert escalate_resp.status_code == 200

    resolve_resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{appeal_id}/review",
        json={"status": "rejected"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_review_nonexistent_appeal_404s(auth_env):
    admin_token = await create_admin_and_get_token(auth_env, ADMIN_EMAIL, PASSWORD)
    resp = await auth_env.client.post(
        f"/api/v1/admin/appeals/{uuid.uuid4()}/review",
        json={"status": "approved"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 404
