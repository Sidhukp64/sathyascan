"""
Integration tests for the Phase 8 PDF report download endpoint
(`GET /api/v1/history/{analysis_id}/report.pdf`, app/api/v1/routers/history.py)
— full HTTP round trips using tests/conftest.py's shared `auth_env` fixture.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.audit_log import AuditLog
from app.models.claim import Claim
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812345801"
PHONE_B = "919812345802"


async def _seed_analysis_with_claim(sessionmaker, user_id: uuid.UUID) -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="A report-able claim.",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result="false",
            language="en",
            privacy_mode_snapshot=True,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.flush()
        session.add(
            Claim(
                analysis_id=analysis.id,
                claim_text="A report-able claim.",
                claim_order=0,
                result="false",
                investigation_complete=True,
                claim_confidence=0.9,
                reasoning_text="Reasoning.",
                category="other",
            )
        )
        await session.commit()
        return analysis.id


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


@pytest.mark.asyncio
async def test_download_report_requires_auth(auth_env):
    resp = await auth_env.client.get(f"/api/v1/history/{uuid.uuid4()}/report.pdf")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_download_report_success(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.get(
        f"/api/v1/history/{analysis_id}/report.pdf", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert "attachment" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_report_cross_user_returns_404(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    resp = await auth_env.client.get(
        f"/api/v1/history/{analysis_id}/report.pdf", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_report_nonexistent_analysis_returns_404(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        f"/api/v1/history/{uuid.uuid4()}/report.pdf", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_report_is_audited(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    analysis_id = await _seed_analysis_with_claim(auth_env.sessionmaker, user_id)

    await auth_env.client.get(
        f"/api/v1/history/{analysis_id}/report.pdf", headers={"Authorization": f"Bearer {token}"}
    )

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.action == "report_downloaded"))
        entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].entity_id == analysis_id
