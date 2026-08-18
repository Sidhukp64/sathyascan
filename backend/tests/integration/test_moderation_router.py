"""
Integration tests for Phase 9 user-facing Moderation reports API
(app/api/v1/routers/moderation.py) — reporting content NOT owned by the
reporter, target-type validation, own-reports-only visibility.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812370001"
PHONE_B = "919812370002"


async def _seed_analysis_for(auth_env, phone: str) -> tuple[str, uuid.UUID]:
    token = await login_and_get_token(auth_env, phone)
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        user_id = result.scalars().one().id
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="reportable claim",
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
        return token, analysis.id


@pytest.mark.asyncio
async def test_moderation_reports_require_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/moderation/reports")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_report_someone_elses_analysis_succeeds(auth_env):
    """The whole point of moderation reports: reporting content NOT owned
    by the caller must NOT be blocked as an ownership violation."""
    _, analysis_b_id = await _seed_analysis_for(auth_env, PHONE_B)
    token_a = await login_and_get_token(auth_env, PHONE_A)

    resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={
            "report_type": "spam",
            "target_type": "analysis",
            "target_analysis_id": str(analysis_b_id),
            "description": "This looks like spam.",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "open"


@pytest.mark.asyncio
async def test_report_nonexistent_analysis_404s(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={
            "report_type": "abuse",
            "target_type": "analysis",
            "target_analysis_id": str(uuid.uuid4()),
            "description": "x",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_report_malicious_url_without_analysis_reference(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={
            "report_type": "malicious_url",
            "target_type": "url",
            "target_url": "https://evil.example.com/phish",
            "description": "This link looks like a phishing page.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_url_target_type_without_target_url_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={"report_type": "malicious_url", "target_type": "url", "description": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422  # pydantic model_validator rejection


@pytest.mark.asyncio
async def test_invalid_report_type_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={"report_type": "not_a_real_type", "target_type": "url", "target_url": "https://x.example", "description": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_reports_only_shows_own_submitted_reports(auth_env):
    """A reporter never sees reports filed by someone else, and — just as
    importantly for this router — never sees reports filed AGAINST their
    own content (that visibility only exists on the admin side)."""
    token_b, analysis_b_id = await _seed_analysis_for(auth_env, PHONE_B)
    token_a = await login_and_get_token(auth_env, PHONE_A)
    await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={
            "report_type": "spam",
            "target_type": "analysis",
            "target_analysis_id": str(analysis_b_id),
            "description": "x",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )

    list_resp = await auth_env.client.get(
        "/api/v1/moderation/reports", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    # PHONE_B (the reported user) sees none of their own — they never
    # submitted anything, and can't see reports filed against them here.
    # Reuses the SAME token _seed_analysis_for already logged in with —
    # calling login_and_get_token a second time for the same phone number
    # within one test would hit the OTP-start cooldown.
    list_resp_b = await auth_env.client.get(
        "/api/v1/moderation/reports", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert list_resp_b.json()["total"] == 0


@pytest.mark.asyncio
async def test_get_own_report_404_for_someone_elses(auth_env):
    token_b, analysis_b_id = await _seed_analysis_for(auth_env, PHONE_B)
    token_a = await login_and_get_token(auth_env, PHONE_A)
    create_resp = await auth_env.client.post(
        "/api/v1/moderation/reports",
        json={
            "report_type": "spam",
            "target_type": "analysis",
            "target_analysis_id": str(analysis_b_id),
            "description": "x",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    report_id = create_resp.json()["id"]

    resp = await auth_env.client.get(
        f"/api/v1/moderation/reports/{report_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404
