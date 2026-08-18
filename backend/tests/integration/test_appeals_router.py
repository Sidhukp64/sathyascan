"""
Integration tests for Phase 9 user-facing Appeals API
(app/api/v1/routers/appeals.py) — ownership, authorization, duplicate
prevention, cancel-only-while-open.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812350001"
PHONE_B = "919812350002"


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


async def _seed_analysis(sessionmaker, user_id: uuid.UUID) -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="A claim worth appealing.",
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
        return analysis.id


@pytest.mark.asyncio
async def test_appeals_require_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/appeals")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_appeal_success(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "I disagree with this verdict."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "open"
    assert body["analysis_id"] == str(analysis_id)


@pytest.mark.asyncio
async def test_create_appeal_404_for_analysis_not_owned(auth_env):
    """IDOR protection — appealing another user's analysis returns 404, not
    403 (same convention as every other ownership check in this codebase)."""
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)

    await login_and_get_token(auth_env, PHONE_B)
    user_b_id = await _get_user_id(auth_env.sessionmaker)
    analysis_b_id = await _seed_analysis(auth_env.sessionmaker, user_b_id)

    resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_b_id), "reason_text": "Not mine but I'll try."},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_open_appeal_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    first = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "First appeal."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201

    second = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "Second appeal, same analysis."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_list_and_get_own_appeal(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    create_resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    appeal_id = create_resp.json()["id"]

    list_resp = await auth_env.client.get("/api/v1/appeals", headers={"Authorization": f"Bearer {token}"})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    get_resp = await auth_env.client.get(
        f"/api/v1/appeals/{appeal_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200


@pytest.mark.asyncio
async def test_get_appeal_404_for_other_users_appeal(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    analysis_a_id = await _seed_analysis(auth_env.sessionmaker, user_a_id)
    create_resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_a_id), "reason_text": "x"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    appeal_id = create_resp.json()["id"]

    token_b = await login_and_get_token(auth_env, PHONE_B)
    resp = await auth_env.client.get(
        f"/api/v1/appeals/{appeal_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_open_appeal_succeeds(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)
    create_resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    appeal_id = create_resp.json()["id"]

    cancel_resp = await auth_env.client.post(
        f"/api/v1/appeals/{appeal_id}/cancel", headers={"Authorization": f"Bearer {token}"}
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_non_open_appeal_rejected(auth_env):
    """Cancel is only valid while status == 'open' — once an admin has
    started reviewing it, a user can no longer unilaterally cancel it."""
    from app.models.appeal import Appeal

    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)
    create_resp = await auth_env.client.post(
        "/api/v1/appeals",
        json={"analysis_id": str(analysis_id), "reason_text": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    appeal_id = create_resp.json()["id"]

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(Appeal).where(Appeal.id == uuid.UUID(appeal_id)))
        appeal = result.scalar_one()
        appeal.status = "under_review"
        await session.commit()

    cancel_resp = await auth_env.client.post(
        f"/api/v1/appeals/{appeal_id}/cancel", headers={"Authorization": f"Bearer {token}"}
    )
    assert cancel_resp.status_code == 400
