"""
Integration tests for the Phase 8 Conversation/Analysis Session API
(app/api/v1/routers/analysis_sessions.py) — full HTTP round trips using
tests/conftest.py's shared `auth_env` fixture.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812346101"
PHONE_B = "919812346102"


async def _seed_analysis(sessionmaker, user_id: uuid.UUID) -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="x",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result="false",
            language="en",
            privacy_mode_snapshot=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.commit()
        return analysis.id


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


@pytest.mark.asyncio
async def test_create_session_requires_auth(auth_env):
    resp = await auth_env.client.post("/api/v1/sessions", json={"name": "Election claims"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_and_get_session(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await auth_env.client.post("/api/v1/sessions", json={"name": "Election claims"}, headers=headers)
    assert create_resp.status_code == 201
    session_id = create_resp.json()["id"]
    assert create_resp.json()["analysis_count"] == 0

    get_resp = await auth_env.client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "Election claims"
    assert get_resp.json()["analyses"] == []


@pytest.mark.asyncio
async def test_get_session_cross_user_returns_404(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    create_resp = await auth_env.client.post(
        "/api/v1/sessions", json={"name": "Private"}, headers={"Authorization": f"Bearer {token_a}"}
    )
    session_id = create_resp.json()["id"]

    token_b = await login_and_get_token(auth_env, PHONE_B)
    resp = await auth_env.client.get(
        f"/api/v1/sessions/{session_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_sessions_isolated_per_user(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    await auth_env.client.post(
        "/api/v1/sessions", json={"name": "A"}, headers={"Authorization": f"Bearer {token_a}"}
    )

    token_b = await login_and_get_token(auth_env, PHONE_B)

    resp_a = await auth_env.client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {token_a}"})
    resp_b = await auth_env.client.get("/api/v1/sessions", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_a.json()["total"] == 1
    assert resp_b.json()["total"] == 0


@pytest.mark.asyncio
async def test_rename_session(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post("/api/v1/sessions", json={"name": "Old name"}, headers=headers)
    session_id = create_resp.json()["id"]

    resp = await auth_env.client.put(f"/api/v1/sessions/{session_id}", json={"name": "New name"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "New name"


@pytest.mark.asyncio
async def test_add_and_remove_analysis(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post("/api/v1/sessions", json={"name": "S"}, headers=headers)
    session_id = create_resp.json()["id"]
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    add_resp = await auth_env.client.post(
        f"/api/v1/sessions/{session_id}/analyses", json={"analysis_id": str(analysis_id)}, headers=headers
    )
    assert add_resp.status_code == 200
    assert add_resp.json()["analysis_count"] == 1
    assert len(add_resp.json()["analyses"]) == 1

    remove_resp = await auth_env.client.delete(
        f"/api/v1/sessions/{session_id}/analyses/{analysis_id}", headers=headers
    )
    assert remove_resp.status_code == 200
    assert remove_resp.json()["analysis_count"] == 0


@pytest.mark.asyncio
async def test_cannot_add_unowned_analysis_to_session(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    create_resp = await auth_env.client.post("/api/v1/sessions", json={"name": "S"}, headers=headers_a)
    session_id = create_resp.json()["id"]

    token_b = await login_and_get_token(auth_env, PHONE_B)
    user_b_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_b_id)

    resp = await auth_env.client.post(
        f"/api/v1/sessions/{session_id}/analyses", json={"analysis_id": str(analysis_id)}, headers=headers_a
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_does_not_delete_its_analyses(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await auth_env.client.post("/api/v1/sessions", json={"name": "S"}, headers=headers)
    session_id = create_resp.json()["id"]
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)
    await auth_env.client.post(
        f"/api/v1/sessions/{session_id}/analyses", json={"analysis_id": str(analysis_id)}, headers=headers
    )

    delete_resp = await auth_env.client.delete(f"/api/v1/sessions/{session_id}", headers=headers)
    assert delete_resp.status_code == 204

    # The analysis itself still exists, just ungrouped.
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(Analysis).where(Analysis.id == analysis_id))
        row = result.scalar_one()
        assert row.session_id is None

    # Session itself is gone.
    get_resp = await auth_env.client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert get_resp.status_code == 404
