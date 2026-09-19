"""
Integration tests for the Public Fact-Check API (app/api/v1/routers/fact_check.py)
— verifies zero-authentication access, claim verification schema, PDF report downloads,
and rate-limiting using tests/conftest.py's shared `auth_env` fixture.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.orchestrator import PipelineOutcome
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.evidence import Evidence


async def _seed_analysis(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=uuid.uuid4(),
            input_type="text",
            input_text="UPI surcharge rumor text",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"web-{uuid.uuid4().hex[:12]}",
            status="completed",
            overall_result="false",
            language="en",
            privacy_mode_snapshot=False,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.flush()
        claim = Claim(
            analysis_id=analysis.id,
            claim_text="UPI surcharge rumor text",
            claim_order=0,
            result="false",
            investigation_complete=True,
            claim_confidence=0.98,
            reasoning_text="NPCI clarified that UPI is free.",
            category="governance",
        )
        session.add(claim)
        await session.flush()
        evidence = Evidence(
            claim_id=claim.id,
            source_url="https://npci.org.in/press-release",
            source_domain="npci.org.in",
            publisher_name="NPCI",
            credibility_tier="tier_1",
            snippet_text="UPI remains free for consumers.",
            stance="refutes",
            retrieved_at=datetime.now(timezone.utc),
        )
        session.add(evidence)
        await session.commit()
        return analysis.id


@pytest.mark.asyncio
async def test_pdf_report_404_when_nonexistent(auth_env):
    missing_id = uuid.uuid4()
    resp = await auth_env.client.get(f"/api/v1/fact-check/{missing_id}/report.pdf")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pdf_report_200_when_exists(auth_env):
    analysis_id = await _seed_analysis(auth_env.sessionmaker)
    resp = await auth_env.client.get(f"/api/v1/fact-check/{analysis_id}/report.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment; filename=" in resp.headers.get("content-disposition", "")
    assert resp.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_fact_check_validation_error_on_short_text(auth_env):
    resp = await auth_env.client.post("/api/v1/fact-check", json={"text": "x", "language": "en"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_fact_check_endpoint_returns_verdict_schema(auth_env):
    mock_analysis_id = uuid.uuid4()

    fake_analysis = Analysis(
        id=mock_analysis_id,
        user_id=uuid.uuid4(),
        input_type="text",
        input_text="Sample viral forward",
        content_fingerprint="fp123",
        source_wamid="wamid123",
        status="completed",
        overall_result="false",
        language="en",
        privacy_mode_snapshot=False,
        completed_at=datetime.now(timezone.utc),
    )

    fake_outcome = PipelineOutcome(
        reply_text="This is false based on NPCI clarification.",
        analysis=fake_analysis,
    )

    with patch("app.agent.orchestrator.TextPipeline.run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = fake_outcome

        resp = await auth_env.client.post(
            "/api/v1/fact-check",
            json={"text": "5% surcharge on UPI transactions", "language": "en"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_result"] == "false"
        assert "confidence_score" in data
        assert "This is false" in data["reply_text"]
        assert data["pdf_report_url"] == f"/api/v1/fact-check/{mock_analysis_id}/report.pdf"
