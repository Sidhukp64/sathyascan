"""
Integration tests for the Phase 8 Overview/Analytics API
(app/api/v1/routers/overview.py) — full HTTP round trips using
tests/conftest.py's shared `auth_env` fixture.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812346001"
PHONE_B = "919812346002"


async def _seed_analysis(sessionmaker, user_id: uuid.UUID, *, input_type="text", result="false", category="health"):
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type=input_type,
            input_text="x",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result=result,
            language="en",
            privacy_mode_snapshot=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.flush()
        session.add(
            Claim(
                analysis_id=analysis.id,
                claim_text="x",
                claim_order=0,
                result=result,
                investigation_complete=True,
                reasoning_text="x",
                category=category,
            )
        )
        await session.commit()


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


@pytest.mark.asyncio
async def test_overview_requires_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/overview")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_overview_empty_for_new_user(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_checks"] == 0
    assert body["checks_by_type"] == {"text": 0, "image": 0, "url": 0, "audio": 0, "video": 0}


@pytest.mark.asyncio
async def test_overview_counts_by_type_and_result(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id, input_type="text", result="verified")
    await _seed_analysis(auth_env.sessionmaker, user_id, input_type="image", result="false")
    await _seed_analysis(auth_env.sessionmaker, user_id, input_type="text", result="unverified")

    resp = await auth_env.client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    assert body["total_checks"] == 3
    assert body["checks_by_type"]["text"] == 2
    assert body["checks_by_type"]["image"] == 1
    assert body["results_by_category"]["credible"] == 1
    assert body["results_by_category"]["misleading_or_false"] == 1
    assert body["results_by_category"]["uncertain"] == 1


@pytest.mark.asyncio
async def test_overview_most_checked_categories(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id, category="health")
    await _seed_analysis(auth_env.sessionmaker, user_id, category="health")
    await _seed_analysis(auth_env.sessionmaker, user_id, category="elections")

    resp = await auth_env.client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token}"})
    categories = {c["category"]: c["count"] for c in resp.json()["most_checked_categories"]}
    assert categories["health"] == 2
    assert categories["elections"] == 1


@pytest.mark.asyncio
async def test_overview_is_isolated_per_user(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_a_id)
    await _seed_analysis(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    user_b_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_b_id)

    resp_a = await auth_env.client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token_a}"})
    resp_b = await auth_env.client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_a.json()["total_checks"] == 2
    assert resp_b.json()["total_checks"] == 1


@pytest.mark.asyncio
async def test_overview_recent_activity_ordering(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id)
    await _seed_analysis(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.get("/api/v1/overview", headers={"Authorization": f"Bearer {token}"})
    recent = resp.json()["recent_activity"]
    assert len(recent) == 2
    # newest first
    assert recent[0]["created_at"] >= recent[1]["created_at"]
