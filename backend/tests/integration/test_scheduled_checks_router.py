"""
Integration tests for the Phase 8 "Check This Tomorrow" API
(app/api/v1/routers/scheduled_checks.py) — full HTTP round trips using
tests/conftest.py's shared `auth_env` fixture, same pattern as the Phase 6
History router tests. Analyses/Claims are seeded directly (this router
surfaces/creates scheduling rows; it doesn't run the fact-checking pipeline
itself).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.scheduled_check import ScheduledCheck
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812345701"
PHONE_B = "919812345702"


async def _seed_analysis_with_claim(sessionmaker, user_id: uuid.UUID, *, result: str = "false") -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="The claim text.",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result=result,
            language="en",
            privacy_mode_snapshot=True,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.flush()

        claim = Claim(
            analysis_id=analysis.id,
            claim_text="The claim text.",
            claim_order=0,
            result=result,
            investigation_complete=True,
            claim_confidence=0.9,
            reasoning_text="Original reasoning.",
            category="other",
        )
        session.add(claim)
        await session.commit()
        return analysis.id


async def _seed_analysis_without_claim(sessionmaker, user_id: uuid.UUID) -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="No claims here.",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result="no_claims_detected",
            language="en",
            privacy_mode_snapshot=True,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.commit()
        return analysis.id


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


@pytest.mark.asyncio
async def test_create_scheduled_check_requires_auth(auth_env):
    resp = await auth_env.client.post("/api/v1/scheduled-checks", json={"source_analysis_id": str(uuid.uuid4())})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_scheduled_check_success(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.post(
        "/api/v1/scheduled-checks",
        json={"source_analysis_id": str(analysis_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["claim_text_snapshot"] == "The claim text."
    assert body["previous_result_snapshot"]["result"] == "false"


@pytest.mark.asyncio
async def test_create_scheduled_check_defaults_to_tomorrow(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.post(
        "/api/v1/scheduled-checks",
        json={"source_analysis_id": str(analysis_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    scheduled_for = datetime.fromisoformat(body["scheduled_for"])
    delta_hours = (scheduled_for - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23 < delta_hours < 25  # ~24h default, allowing for test execution time


@pytest.mark.asyncio
async def test_create_scheduled_check_rejects_analysis_with_no_claims(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_without_claim(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.post(
        "/api/v1/scheduled-checks",
        json={"source_analysis_id": str(analysis_id)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_scheduled_check_rejects_unowned_analysis(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)

    resp = await auth_env.client.post(
        "/api/v1/scheduled-checks",
        json={"source_analysis_id": str(analysis_id)},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_scheduling_is_prevented(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}

    first = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    assert first.status_code == 201

    second = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_list_scheduled_checks_isolated_per_user(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    analysis_a = await _seed_analysis_with_claim(auth_env.sessionmaker, user_a_id)
    await auth_env.client.post(
        "/api/v1/scheduled-checks",
        json={"source_analysis_id": str(analysis_a)},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    token_b = await login_and_get_token(auth_env, PHONE_B)

    resp_a = await auth_env.client.get("/api/v1/scheduled-checks", headers={"Authorization": f"Bearer {token_a}"})
    resp_b = await auth_env.client.get("/api/v1/scheduled-checks", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_a.json()["total"] == 1
    assert resp_b.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_scheduled_checks_filters_by_status(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}
    await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )

    resp_pending = await auth_env.client.get(
        "/api/v1/scheduled-checks", params={"status": "pending"}, headers=headers
    )
    resp_completed = await auth_env.client.get(
        "/api/v1/scheduled-checks", params={"status": "completed"}, headers=headers
    )
    assert resp_pending.json()["total"] == 1
    assert resp_completed.json()["total"] == 0


@pytest.mark.asyncio
async def test_get_scheduled_check_by_id(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    check_id = create_resp.json()["id"]

    resp = await auth_env.client.get(f"/api/v1/scheduled-checks/{check_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == check_id


@pytest.mark.asyncio
async def test_get_scheduled_check_cross_user_returns_404(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_a_id)
    create_resp = await auth_env.client.post(
        "/api/v1/scheduled-checks",
        json={"source_analysis_id": str(analysis_id)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    check_id = create_resp.json()["id"]

    token_b = await login_and_get_token(auth_env, PHONE_B)
    resp = await auth_env.client.get(
        f"/api/v1/scheduled-checks/{check_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_pending_scheduled_check(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    check_id = create_resp.json()["id"]

    resp = await auth_env.client.delete(f"/api/v1/scheduled-checks/{check_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cannot_cancel_already_cancelled_check(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    check_id = create_resp.json()["id"]
    await auth_env.client.delete(f"/api/v1/scheduled-checks/{check_id}", headers=headers)

    resp = await auth_env.client.delete(f"/api/v1/scheduled-checks/{check_id}", headers=headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_only_allowed_on_failed_check(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    check_id = create_resp.json()["id"]

    # Still 'pending' — retry must be rejected.
    resp = await auth_env.client.post(f"/api/v1/scheduled-checks/{check_id}/retry", headers=headers)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_resets_a_failed_check_to_pending(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post(
        "/api/v1/scheduled-checks", json={"source_analysis_id": str(analysis_id)}, headers=headers
    )
    check_id = create_resp.json()["id"]

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(ScheduledCheck).where(ScheduledCheck.id == uuid.UUID(check_id)))
        row = result.scalar_one()
        row.status = "failed"
        row.attempts = 3
        row.error_message = "boom"
        await session.commit()

    resp = await auth_env.client.post(f"/api/v1/scheduled-checks/{check_id}/retry", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["error_message"] is None
