"""
Public Fact-Checking API for the SathyaScan Web Platform.
Allows any citizen or web user to submit claims or URLs for immediate verification,
view rich evidentiary verdicts, and download cryptographic fact-sheet PDF reports.
"""

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.claim_pipeline_shared import load_verdicts
from app.agent.orchestrator import PipelineDependencies, TextPipeline
from app.agent.pdf_report import generate_analysis_report_pdf
from app.agent.url_detection import extract_bare_url
from app.agent.url_pipeline import UrlPipeline, UrlPipelineDependencies
from app.agent.user_service import get_or_create_user
from app.api.v1.deps import get_db_session, get_redis
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimiter
from app.integrations.claude_client import LLMClient
from app.integrations.evidence_search_client import EvidenceSearchProvider
from app.models.analysis import Analysis
from app.agent.tools.url_analyzer import UrlAnalyzerTool
from app.agent.tools.url_safety import UrlSafetyTool
from app.web.fetcher import SecureUrlFetcher
from app.webhook.whatsapp.router import (
    get_llm_client,
    get_search_provider,
    get_url_analyzer_tool,
    get_url_fetcher,
    get_url_safety_tool,
)

router = APIRouter(prefix="/api/v1/fact-check", tags=["fact-check"])


class FactCheckRequest(BaseModel):
    text: str = Field(..., min_length=2, max_length=5000, description="Claim text or URL to verify")
    language: str = Field(default="en", description="Preferred response language: en, ml, hi, ta")


class FactCheckResponse(BaseModel):
    analysis_id: UUID | None = None
    overall_result: str
    confidence_score: float
    reply_text: str
    headline: str
    claims: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    audio_verdict_summary: str
    pdf_report_url: str | None = None
    is_url: bool = False


async def _enforce_public_rate_limit(request: Request, redis: Redis, settings: Settings) -> None:
    client_ip = request.client.host if request.client else "unknown"
    limiter = RateLimiter(redis, settings.explore_rate_limit_per_ip_per_minute)
    result = await limiter.check_and_increment(f"factcheck-ip:{client_ip}")
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification requests. Please slow down and try again shortly.",
        )


@router.post("", response_model=FactCheckResponse)
async def check_claim_or_url(
    req: FactCheckRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm_client),
    search_provider: EvidenceSearchProvider = Depends(get_search_provider),
    url_fetcher: SecureUrlFetcher = Depends(get_url_fetcher),
    url_analyzer: UrlAnalyzerTool = Depends(get_url_analyzer_tool),
    url_safety: UrlSafetyTool = Depends(get_url_safety_tool),
) -> FactCheckResponse:
    await _enforce_public_rate_limit(request, redis, settings)

    language = req.language if req.language in {"en", "ml", "hi", "ta"} else "en"
    user = await get_or_create_user(
        session,
        "public_web_citizen",
        settings.phone_hash_pepper,
        settings.phone_encryption_key,
        default_language=language,
    )

    clean_text = req.text.strip()
    bare_url = extract_bare_url(clean_text)
    is_url = bare_url is not None
    source_wamid = f"web-{uuid4().hex[:12]}"

    if is_url:
        url_deps = UrlPipelineDependencies(
            fetcher=url_fetcher,
            url_analyzer=url_analyzer,
            url_safety=url_safety,
            llm=llm,
            search_provider=search_provider,
            redis=redis,
        )
        url_pipeline = UrlPipeline(session, url_deps, settings)
        outcome = await url_pipeline.run(
            user=user,
            url=bare_url,
            source_wamid=source_wamid,
            language=language,
        )
    else:
        text_deps = PipelineDependencies(llm=llm, search_provider=search_provider, redis=redis)
        text_pipeline = TextPipeline(session, text_deps, settings)
        outcome = await text_pipeline.run(
            user=user,
            text=clean_text,
            source_wamid=source_wamid,
            language=language,
        )

    claims_data: list[dict[str, Any]] = []
    evidence_data: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    confidence_score = 0.85
    overall_result = "unverified"
    pdf_report_url = None

    if outcome.analysis is not None:
        overall_result = outcome.analysis.overall_result or "unverified"
        verdicts = await load_verdicts(session, outcome.analysis.id)
        if verdicts:
            confidence_score = max((float(v.confidence) for v in verdicts), default=0.85)
            for v in verdicts:
                claims_data.append({
                    "claim_text": v.claim_text,
                    "result": v.result,
                    "confidence": float(v.confidence) if v.confidence is not None else 0.85,
                    "reasoning": v.reasoning_text,
                    "category": v.category,
                })
                for e in v.evidence:
                    if e.source_url not in seen_urls:
                        seen_urls.add(e.source_url)
                        evidence_data.append({
                            "publisher_name": e.publisher_name or e.source_domain,
                            "source_url": e.source_url,
                            "source_domain": e.source_domain,
                            "credibility_tier": e.credibility_tier,
                            "snippet_text": e.snippet_text,
                            "stance": e.stance,
                        })

        pdf_report_url = f"/api/v1/fact-check/{outcome.analysis.id}/report.pdf"

    headline = clean_text if len(clean_text) <= 180 else clean_text[:177] + "..."
    audio_summary = (
        outcome.reply_text
        if len(outcome.reply_text) <= 300
        else outcome.reply_text[:297] + "..."
    )

    return FactCheckResponse(
        analysis_id=outcome.analysis.id if outcome.analysis else None,
        overall_result=overall_result,
        confidence_score=round(confidence_score, 2),
        reply_text=outcome.reply_text,
        headline=headline,
        claims=claims_data,
        evidence=evidence_data,
        audio_verdict_summary=audio_summary,
        pdf_report_url=pdf_report_url,
        is_url=is_url,
    )


@router.get("/{analysis_id}/report.pdf")
async def download_public_report_pdf(
    analysis_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    result = await session.execute(
        select(Analysis).where(Analysis.id == analysis_id, Analysis.is_deleted.is_(False))
    )
    analysis = result.scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis report not found.")

    pdf_bytes = await generate_analysis_report_pdf(session, analysis)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="sathyascan-report-{analysis.id}.pdf"'},
    )
