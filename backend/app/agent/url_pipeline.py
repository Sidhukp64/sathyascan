"""
UrlPipeline — Phase 4's orchestrator. Mirrors ImagePipeline's shape and
reuses the same infrastructure (app/agent/claim_pipeline_shared.py,
AnalysisBudgetGuard, GlobalConcurrencyGuard, the circuit breaker) — the only
genuinely new logic is the URL-specific front half (SSRF pre-check,
robots.txt, fetch, content extraction, safety assessment).

Flow (matches phased-plan.md's Phase 4 done-when criterion: "forwarding a
URL returns both a credibility assessment and a distinct safety-risk
block"):

  url
    -> SSRF pre-check on the ORIGINAL host (cheap, no network — decides
       whether robots.txt/the main fetch are even attempted)
    -> robots.txt check (decisions.md §3) — skipped if SSRF-blocked
    -> ONE SSRF-protected fetch (app/web/fetcher.py — re-checks SSRF on
       EVERY redirect hop, not just the first URL), skipped if robots.txt
       disallows it
    -> URL Safety assessment (ALWAYS run, even if the fetch never
       happened — an SSRF-blocked or robots-disallowed URL is itself a
       real, complete safety result, not a failure) -> persisted to
       url_safety_scans
    -> content extraction from the SAME fetched bytes (no second fetch) ->
       claim-extraction / evidence-investigation / classification through
       the EXISTING Phase 2 shared pipeline (only if content was actually
       fetched and is HTML)
    -> ONE combined reply: safety block ALWAYS present, credibility block
       present whenever claims exist

Cost accounting: the fetch consumes ONE unit of the SAME
AnalysisBudgetGuard.llm_tool_call_count counter text/image claims use — no
second, parallel budget system. URL Safety's heuristics are pure, local
computation (no LLM calls, no evidence searches) and consume no budget at
all — deliberately, since they cost nothing to run.

Analysis outcome semantics are intentionally different from ImagePipeline's:
Phase 4 ALWAYS has a genuine deliverable (the safety block) even when the
content fetch fails entirely, so a failed/blocked/disallowed fetch still
resolves to `analysis.status = "completed"` (a real, useful reply was sent)
with `overall_result = "insufficient_evidence"` (no claim investigation
could begin) rather than `status = "failed"` — unlike Phase 3, where a
download failure leaves nothing useful to send at all.

Duplicate-content reuse is explicitly OUT of Phase 4 scope, same as
ImagePipeline's Phase 3 scope decision — every URL is freshly analyzed.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget_guard import AnalysisBudgetGuard, BudgetExceededError
from app.agent.claim_pipeline_shared import aggregate_overall_result, finalize_usage, investigate_claim, persist_claim
from app.agent.orchestrator import PipelineOutcome
from app.agent.schemas import ClaimVerdict
from app.agent.tools.claim_extraction import ClaimExtractionTool
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.tools.response_generation import ResponseGenerationTool
from app.agent.tools.source_evaluation import SourceEvaluationTool
from app.agent.tools.url_analyzer import UrlAnalyzerTool, UrlContentResult, UrlContentStatus
from app.agent.tools.url_safety import UrlSafetyTool
from app.agent.usage_tracking import is_circuit_breaker_tripped
from app.core.concurrency import ConcurrencyLimitReachedError, GlobalConcurrencyGuard
from app.core.config import Settings
from app.core.logging import log_event
from app.core.ssrf_guard import SSRFBlockedError, resolve_and_check_hostname
from app.integrations.claude_client import LLMClient, LLMProviderError, LLMProviderTimeout
from app.models.analysis import Analysis
from app.models.url_safety_scan import UrlSafetyScan
from app.models.user import User
from app.web.fetcher import (
    FetchResult,
    SecureUrlFetcher,
    TooManyRedirectsError,
    UnsafeUrlError,
    UrlFetchError,
    UrlFetchTimeout,
    UrlTooLargeError,
)
from app.web.robots import is_allowed_by_robots_txt

logger = logging.getLogger(__name__)


@dataclass
class UrlPipelineDependencies:
    llm: LLMClient
    search_provider: object  # EvidenceSearchProvider — loosely typed, matches PipelineDependencies
    redis: object
    url_fetcher: SecureUrlFetcher
    url_analyzer_tool: UrlAnalyzerTool
    url_safety_tool: UrlSafetyTool
    # Test-only injection point for the robots.txt check's own httpx client
    # (httpx.MockTransport) — None means the real network transport, same
    # pattern as SecureUrlFetcher's own `transport` param. Kept separate
    # from url_fetcher's internal transport since robots.py owns its own
    # httpx.AsyncClient rather than reusing SecureUrlFetcher's.
    robots_transport: object = None


class UrlPipeline:
    def __init__(self, session: AsyncSession, deps: UrlPipelineDependencies, settings: Settings) -> None:
        self._session = session
        self._deps = deps
        self._settings = settings
        self._response_tool = ResponseGenerationTool()

    async def run(self, *, user: User, url: str, source_wamid: str, language: str) -> PipelineOutcome:
        if await is_circuit_breaker_tripped(self._session, self._settings.daily_spend_circuit_breaker_usd):
            log_event(logger, logging.WARNING, "daily circuit breaker tripped, declining URL analysis")
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
            log_event(logger, logging.WARNING, "global concurrency limit reached, declining URL analysis")
            return PipelineOutcome(
                reply_text=self._response_tool.generate_at_capacity_reply(language),
                analysis=None,
                declined=True,
                decline_reason="concurrency_limit",
            )

        try:
            return await self._run_full_analysis(user, url, source_wamid, language)
        finally:
            await concurrency_guard.release()

    async def _run_full_analysis(self, user: User, url: str, source_wamid: str, language: str) -> PipelineOutcome:
        started_at = datetime.now(timezone.utc)

        analysis = Analysis(
            user_id=user.id,
            input_type="url",
            input_text=url,  # the URL itself, NEVER the fetched page content (see module docstring / decisions.md §3)
            content_fingerprint=hashlib.sha256(url.encode()).hexdigest(),
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

        fetch_result, ssrf_blocked_host, robots_disallowed, fetch_error_code = await self._fetch_url(
            url, budget_guard
        )

        content_result = self._deps.url_analyzer_tool.analyze(
            fetch_result, max_text_length_chars=self._settings.max_url_content_length_chars
        )

        safety_result = await self._deps.url_safety_tool.assess(
            url,
            fetch_result=fetch_result,
            page_title=content_result.title if content_result.status == UrlContentStatus.SUCCESS else None,
            page_text=content_result.extracted_text if content_result.status == UrlContentStatus.SUCCESS else None,
            ssrf_blocked_host=ssrf_blocked_host,
        )

        recommendation_text = self._response_tool.render_url_recommendation(safety_result.risk_level.value, language)
        self._session.add(
            UrlSafetyScan(
                analysis_id=analysis.id,
                url=url,
                risk_level=safety_result.risk_level.value,
                reasons=[{"code": f.code.value, "params": f.params} for f in safety_result.findings],
                recommendation=recommendation_text,
                threat_intel_matches=(
                    [{"list_name": m.list_name, "detail": m.detail} for m in safety_result.threat_intel_matches]
                    or None
                ),
                scanned_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()

        verdicts: list[ClaimVerdict] = []
        error_code = fetch_error_code

        if content_result.status == UrlContentStatus.SUCCESS:
            verdicts, extraction_error_code = await self._investigate_claims(
                content_result, budget_guard, language, analysis.id
            )
            error_code = extraction_error_code or error_code

        analysis.status = "completed"
        analysis.overall_result = self._compute_overall_result(content_result, verdicts)
        if error_code:
            analysis.error_code = error_code
        analysis.completed_at = datetime.now(timezone.utc)
        await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)

        reply_text = self._response_tool.generate_url_analysis_reply(
            url,
            safety_result,
            verdicts,
            content_result.status,
            language,
            robots_disallowed=robots_disallowed,
        )
        return PipelineOutcome(reply_text=reply_text, analysis=analysis)

    async def _fetch_url(
        self, url: str, budget_guard: AnalysisBudgetGuard
    ) -> tuple[FetchResult | None, str | None, bool, str | None]:
        """Returns (fetch_result, ssrf_blocked_host, robots_disallowed,
        error_code). At most one of fetch_result / ssrf_blocked_host /
        robots_disallowed=True / error_code is meaningfully set — the others
        stay None/False."""
        hostname = urlparse(url).hostname or ""
        try:
            resolve_and_check_hostname(hostname)
        except SSRFBlockedError:
            log_event(logger, logging.WARNING, "URL blocked before fetch (SSRF)", host=hostname)
            return None, hostname, False, "unsafe_url"

        if self._settings.url_robots_txt_check_enabled:
            try:
                allowed = await is_allowed_by_robots_txt(
                    url,
                    self._settings.url_fetch_user_agent,
                    self._settings.url_fetch_timeout_seconds,
                    transport=self._deps.robots_transport,
                )
            except Exception:  # noqa: BLE001 - a broken robots-check must never block a fetch
                log_event(logger, logging.INFO, "robots.txt check raised unexpectedly, defaulting to allowed")
                allowed = True
            if not allowed:
                log_event(logger, logging.INFO, "URL fetch skipped: disallowed by robots.txt")
                return None, None, True, "robots_disallowed"

        try:
            budget_guard.record_llm_call()
        except BudgetExceededError:
            return None, None, False, "tool_call_limit"

        try:
            fetch_result = await self._deps.url_fetcher.fetch(url)
            return fetch_result, None, False, None
        except UnsafeUrlError as exc:
            log_event(logger, logging.WARNING, "URL fetch blocked mid-redirect (SSRF)", host=exc.blocked_host)
            return None, exc.blocked_host or hostname, False, "unsafe_url"
        except UrlFetchTimeout:
            return None, None, False, "timeout"
        except UrlTooLargeError:
            return None, None, False, "file_too_large"
        except TooManyRedirectsError:
            return None, None, False, "too_many_redirects"
        except UrlFetchError:
            return None, None, False, "download_failed"

    async def _investigate_claims(
        self, content_result: UrlContentResult, budget_guard: AnalysisBudgetGuard, language: str, analysis_id
    ) -> tuple[list[ClaimVerdict], str | None]:
        extraction_tool = ClaimExtractionTool(self._deps.llm)
        try:
            budget_guard.record_llm_call()
            extraction = await extraction_tool.extract(content_result.extracted_text, language)
        except BudgetExceededError:
            return [], "tool_call_limit"
        except LLMProviderTimeout:
            return [], "timeout"
        except LLMProviderError:
            return [], "provider_unavailable"

        if not extraction.claims:
            return [], None

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
            await persist_claim(self._session, analysis_id, order, verdict)

        return verdicts, None

    @staticmethod
    def _compute_overall_result(content_result: UrlContentResult, verdicts: list[ClaimVerdict]) -> str:
        if verdicts:
            return aggregate_overall_result(verdicts)
        if content_result.status in (UrlContentStatus.SUCCESS, UrlContentStatus.NO_TEXT_FOUND, UrlContentStatus.UNSUPPORTED_CONTENT_TYPE):
            return "no_claims_detected"
        return "insufficient_evidence"  # FETCH_FAILED — investigation never began
