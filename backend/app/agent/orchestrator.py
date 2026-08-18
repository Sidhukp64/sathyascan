"""
TextPipeline — the deterministic orchestrator tying every Phase 2 component
together, in exactly the order specified for Phase 2:

WhatsApp text -> normalization -> claim detection/extraction -> duplicate
check -> budget guard -> evidence search -> source credibility -> evidence
extraction -> evidence comparison -> completeness check -> evidence-tier
check -> verdict classification -> confidence/reasoning -> multilingual
response -> WhatsApp response.

This module is deliberately "boring" Python control flow — all judgment
calls happen inside the tools it calls; this file only decides *when* to
call them and *what to do with a failure*, never *what the answer is*.

Phase 3 note: the per-claim investigation/persistence/usage-accounting
logic previously private to this class now lives in
app/agent/claim_pipeline_shared.py (a pure mechanical extraction, no
behavior change — see that module's docstring) so app/agent/image_pipeline.py
can reuse it instead of duplicating it. This class's public interface
(`TextPipeline.run(...)`) is unchanged.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget_guard import AnalysisBudgetGuard, BudgetExceededError
from app.agent.claim_pipeline_shared import (
    aggregate_overall_result,
    finalize_usage,
    investigate_claim,
    load_verdicts,
    persist_claim,
)
from app.agent.duplicate_detection import compute_fingerprint, find_reusable_analysis
from app.agent.schemas import ClaimVerdict
from app.agent.tools.claim_extraction import ClaimExtractionTool
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.tools.response_generation import ResponseGenerationTool
from app.agent.tools.source_evaluation import SourceEvaluationTool
from app.agent.usage_tracking import is_circuit_breaker_tripped
from app.core.concurrency import ConcurrencyLimitReachedError, GlobalConcurrencyGuard
from app.core.config import Settings
from app.core.logging import log_event
from app.integrations.claude_client import LLMClient, LLMProviderError, LLMProviderTimeout
from app.models.analysis import Analysis
from app.models.user import User

logger = logging.getLogger(__name__)


@dataclass
class PipelineDependencies:
    llm: LLMClient
    search_provider: object  # EvidenceSearchProvider — typed loosely to avoid an import cycle
    redis: object  # redis.asyncio.Redis


@dataclass
class PipelineOutcome:
    reply_text: str
    analysis: Analysis | None
    reused: bool = False
    declined: bool = False
    decline_reason: str | None = None  # "circuit_breaker" | "concurrency_limit"


class TextPipeline:
    def __init__(self, session: AsyncSession, deps: PipelineDependencies, settings: Settings) -> None:
        self._session = session
        self._deps = deps
        self._settings = settings
        self._response_tool = ResponseGenerationTool()

    async def run(self, *, user: User, text: str, source_wamid: str, language: str) -> PipelineOutcome:
        content_fingerprint = compute_fingerprint(text)

        if self._settings.duplicate_content_reuse_enabled:
            existing = await find_reusable_analysis(
                self._session,
                user.id,
                content_fingerprint,
                language,
                self._settings.duplicate_content_reuse_window_hours,
            )
            if existing is not None:
                log_event(logger, logging.INFO, "duplicate content reused", analysis_id=str(existing.id))
                verdicts = await load_verdicts(self._session, existing.id)
                reply_text = self._response_tool.generate(verdicts, language)
                return PipelineOutcome(reply_text=reply_text, analysis=existing, reused=True)

        if await is_circuit_breaker_tripped(self._session, self._settings.daily_spend_circuit_breaker_usd):
            log_event(logger, logging.WARNING, "daily circuit breaker tripped, declining analysis")
            return PipelineOutcome(
                reply_text=self._response_tool.generate_at_capacity_reply(language),
                analysis=None,
                declined=True,
                decline_reason="circuit_breaker",
            )

        concurrency_guard = GlobalConcurrencyGuard(self._deps.redis, self._settings.global_llm_search_concurrency_limit)
        try:
            await concurrency_guard.acquire()
        except ConcurrencyLimitReachedError:
            log_event(logger, logging.WARNING, "global concurrency limit reached, declining analysis")
            return PipelineOutcome(
                reply_text=self._response_tool.generate_at_capacity_reply(language),
                analysis=None,
                declined=True,
                decline_reason="concurrency_limit",
            )

        try:
            return await self._run_full_analysis(user, text, source_wamid, language, content_fingerprint)
        finally:
            await concurrency_guard.release()

    async def _run_full_analysis(
        self, user: User, text: str, source_wamid: str, language: str, content_fingerprint: str
    ) -> PipelineOutcome:
        started_at = datetime.now(timezone.utc)

        analysis = Analysis(
            user_id=user.id,
            input_type="text",
            input_text=text,
            content_fingerprint=content_fingerprint,
            source_wamid=source_wamid,
            status="processing",
            language=language,
            privacy_mode_snapshot=user.privacy_mode,
        )
        self._session.add(analysis)
        await self._session.flush()

        budget_guard = AnalysisBudgetGuard(
            self._settings.max_evidence_searches_per_analysis,
            self._settings.max_llm_tool_calls_per_analysis,
        )

        extraction_tool = ClaimExtractionTool(self._deps.llm)
        try:
            budget_guard.record_llm_call()
            extraction = await extraction_tool.extract(text, language)
        except BudgetExceededError:
            return await self._finish_with_processing_error(analysis, budget_guard, started_at, language, "tool_call_limit")
        except LLMProviderTimeout:
            return await self._finish_with_processing_error(analysis, budget_guard, started_at, language, "timeout")
        except LLMProviderError:
            return await self._finish_with_processing_error(
                analysis, budget_guard, started_at, language, "provider_unavailable"
            )

        if not extraction.claims:
            analysis.status = "completed"
            analysis.overall_result = "no_claims_detected"
            analysis.completed_at = datetime.now(timezone.utc)
            await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
            return PipelineOutcome(
                reply_text=self._response_tool.generate_no_claims_reply(language), analysis=analysis
            )

        source_evaluator = SourceEvaluationTool(self._session)
        evidence_search_tool = EvidenceSearchTool(self._deps.search_provider, source_evaluator)
        synthesis_tool = EvidenceSynthesisTool(self._deps.llm)

        verdicts: list[ClaimVerdict] = []
        for order, extracted_claim in enumerate(extraction.claims):
            verdict = await investigate_claim(
                self._deps.llm,
                self._settings,
                extracted_claim.text,
                extracted_claim.category,
                budget_guard,
                evidence_search_tool,
                synthesis_tool,
                language,
            )
            verdicts.append(verdict)
            await persist_claim(self._session, analysis.id, order, verdict)

        analysis.status = "completed"
        analysis.overall_result = aggregate_overall_result(verdicts)
        analysis.completed_at = datetime.now(timezone.utc)
        await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)

        return PipelineOutcome(reply_text=self._response_tool.generate(verdicts, language), analysis=analysis)

    async def _finish_with_processing_error(
        self,
        analysis: Analysis,
        budget_guard: AnalysisBudgetGuard,
        started_at: datetime,
        language: str,
        error_code: str,
    ) -> PipelineOutcome:
        analysis.status = "failed"
        analysis.error_code = error_code
        analysis.overall_result = "insufficient_evidence"
        await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
        return PipelineOutcome(
            reply_text=self._response_tool.generate_processing_error_reply(language), analysis=analysis
        )
