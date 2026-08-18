"""
ImagePipeline — Phase 3's orchestrator. Mirrors TextPipeline's shape and
reuses its infrastructure directly (app/agent/claim_pipeline_shared.py,
AnalysisBudgetGuard, GlobalConcurrencyGuard, the circuit breaker) rather
than duplicating it — the only genuinely new logic here is the media-
specific front half (download, validate, safety gate, OCR).

Flow (matches architecture.md's Task 2 -> Task 2.5 -> Task 3 ordering,
whatsapp-integration.md's media-handling section, and agent-architecture.md's
safety-gate section — now implemented, not just diagrammed):

  media_id
    -> download (SSRF-protected, size-capped, no redirects)
    -> validate actual content (magic bytes + decompression-bomb guard)
    -> SAFETY GATE (non-bypassable; BLOCKED/ERROR both stop everything)
    -> OCR (budget-guarded, reuses the SAME counter/limit as text)
    -> [no usable text -> friendly "nothing to check" reply, done]
    -> best-effort image/AI-generation analysis (never gates classification
       — see app/agent/tools/image_analysis.py's module docstring)
    -> OCR'd text fed through the EXISTING Phase 2 claim-extraction /
       evidence-investigation / classification pipeline
    -> response

Cost accounting: OCR and image-analysis calls each consume ONE unit of the
SAME AnalysisBudgetGuard.llm_tool_call_count counter text claims use — no
second, parallel budget system (per the Phase 3 instruction to reuse
existing infrastructure).
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget_guard import AnalysisBudgetGuard, BudgetExceededError
from app.agent.claim_pipeline_shared import aggregate_overall_result, finalize_usage, investigate_claim, persist_claim
from app.agent.orchestrator import PipelineOutcome
from app.agent.retention import compute_media_retention_expiry
from app.agent.safety_gate import SafetyGate
from app.agent.schemas import ClaimVerdict
from app.agent.tools.claim_extraction import ClaimExtractionTool
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.tools.image_analysis import ImageAnalysisResult, ImageAnalysisStatus, ImageAnalysisTool
from app.agent.tools.ocr import OCRStatus, OCRTool
from app.agent.tools.response_generation import ResponseGenerationTool
from app.agent.tools.source_evaluation import SourceEvaluationTool
from app.agent.usage_tracking import is_circuit_breaker_tripped
from app.core.concurrency import ConcurrencyLimitReachedError, GlobalConcurrencyGuard
from app.core.config import Settings
from app.core.logging import log_event
from app.i18n.templates import UI_STRINGS, resolve_language
from app.integrations.claude_client import LLMClient, LLMProviderError, LLMProviderTimeout
from app.media.downloader import (
    MediaDownloadError,
    MediaDownloadTimeout,
    MediaTooLargeError,
    MetaMediaClient,
    UnsafeMediaURLError,
)
from app.media.validation import MediaValidationError, validate_image
from app.models.analysis import Analysis
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.safety_gate_event import SafetyGateEvent
from app.models.user import User

logger = logging.getLogger(__name__)

# incomplete_reason values, per decisions.md §1A: search_limit | tool_call_limit |
# provider_unavailable | timeout | infra_error | other
_OCR_STATUS_TO_INCOMPLETE_REASON = {
    OCRStatus.PROVIDER_UNAVAILABLE: "provider_unavailable",
    OCRStatus.FAILED: "infra_error",
    OCRStatus.LIMIT_REACHED: "tool_call_limit",
}


@dataclass
class ImagePipelineDependencies:
    llm: LLMClient
    search_provider: object  # EvidenceSearchProvider — loosely typed, matches PipelineDependencies
    redis: object
    media_client: MetaMediaClient
    safety_gate: SafetyGate
    ocr_tool: OCRTool
    image_analysis_tool: ImageAnalysisTool


class ImagePipeline:
    def __init__(self, session: AsyncSession, deps: ImagePipelineDependencies, settings: Settings) -> None:
        self._session = session
        self._deps = deps
        self._settings = settings
        self._response_tool = ResponseGenerationTool()

    async def run(self, *, user: User, media_id: str, source_wamid: str, language: str) -> PipelineOutcome:
        # Duplicate-content reuse for images is out of Phase 3 scope
        # (explicitly not requested) — every image is freshly analyzed.

        if await is_circuit_breaker_tripped(self._session, self._settings.daily_spend_circuit_breaker_usd):
            log_event(logger, logging.WARNING, "daily circuit breaker tripped, declining image analysis")
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
            log_event(logger, logging.WARNING, "global concurrency limit reached, declining image analysis")
            return PipelineOutcome(
                reply_text=self._response_tool.generate_at_capacity_reply(language),
                analysis=None,
                declined=True,
                decline_reason="concurrency_limit",
            )

        try:
            return await self._run_full_analysis(user, media_id, source_wamid, language)
        finally:
            await concurrency_guard.release()

    async def _run_full_analysis(self, user: User, media_id: str, source_wamid: str, language: str) -> PipelineOutcome:
        started_at = datetime.now(timezone.utc)

        # content_fingerprint is NOT NULL; the real content-based fingerprint
        # (sha256 of the downloaded bytes) is only known after a successful
        # download, so this row starts with a media_id-derived placeholder
        # and is updated below once we have real bytes.
        analysis = Analysis(
            user_id=user.id,
            input_type="image",
            input_text="",
            content_fingerprint=hashlib.sha256(media_id.encode()).hexdigest(),
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

        # --- Download (SSRF-protected, size-capped, no redirects) ---
        try:
            image_bytes, _claimed_mime_type = await self._deps.media_client.download_media(media_id)
        except MediaTooLargeError:
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "file_too_large",
                self._response_tool.generate_file_too_large_reply(language),
            )
        except MediaDownloadTimeout:
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "timeout",
                self._response_tool.generate_processing_error_reply(language),
            )
        except UnsafeMediaURLError:
            log_event(logger, logging.WARNING, "media download blocked (unsafe URL/SSRF/redirect)")
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "unsafe_url",
                self._response_tool.generate_processing_error_reply(language),
            )
        except MediaDownloadError:
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "download_failed",
                self._response_tool.generate_processing_error_reply(language),
            )

        analysis.content_fingerprint = hashlib.sha256(image_bytes).hexdigest()

        # --- Validate actual content (magic bytes + decompression-bomb guard) ---
        try:
            actual_mime_type = validate_image(image_bytes, self._settings.max_image_pixels)
        except MediaValidationError as exc:
            reply = (
                self._response_tool.generate_unsupported_media_reply(language)
                if exc.reason == "unsupported_format"
                else self._response_tool.generate_processing_error_reply(language)
            )
            return await self._finish_pre_analysis_error(analysis, budget_guard, started_at, language, exc.reason, reply)

        media_attachment = MediaAttachment(
            analysis_id=analysis.id,
            whatsapp_media_id=media_id,
            media_type="image",
            mime_type=actual_mime_type,  # the real sniffed type — never the claimed one
            file_size_bytes=len(image_bytes),
            sha256_hash=bytes.fromhex(analysis.content_fingerprint),
            # Phase 6 — Privacy Mode retention (app/agent/retention.py's purge
            # job scans by this column). Computed from THIS analysis's own
            # privacy_mode_snapshot, not the user's current live setting —
            # matches analyses.privacy_mode_snapshot's own "captured at
            # analysis time, immutable" rule (decisions.md §8).
            retention_expires_at=compute_media_retention_expiry(
                analysis.created_at, analysis.privacy_mode_snapshot, self._settings
            ),
        )
        self._session.add(media_attachment)
        await self._session.flush()

        # --- SAFETY GATE — non-bypassable, before any analysis touches the image ---
        safety_result = await self._deps.safety_gate.check(image_bytes, actual_mime_type)
        self._session.add(
            SafetyGateEvent(
                media_attachment_id=media_attachment.id,
                analysis_id=analysis.id,
                check_type=safety_result.check_type,
                outcome=safety_result.outcome.value,
                provider_name=safety_result.provider_name,
                reference_id=safety_result.reference_id,
            )
        )
        await self._session.flush()

        if not SafetyGate.may_proceed(safety_result):
            log_event(logger, logging.WARNING, "safety gate did not pass", outcome=safety_result.outcome.value)
            analysis.status = "blocked"
            await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
            # Deliberately generic — never expose safety-gate internals (decisions.md §6/§15).
            return PipelineOutcome(
                reply_text=self._response_tool.generate_content_declined_reply(language), analysis=analysis
            )

        # --- OCR (budget-guarded, same counter/limit as text claims) ---
        try:
            budget_guard.record_llm_call()
        except BudgetExceededError:
            return await self._finish_incomplete(analysis, budget_guard, started_at, language, "tool_call_limit")

        ocr_result = await self._deps.ocr_tool.extract(image_bytes, actual_mime_type, language_hint=language)

        if ocr_result.status in _OCR_STATUS_TO_INCOMPLETE_REASON:
            return await self._finish_incomplete(
                analysis, budget_guard, started_at, language, _OCR_STATUS_TO_INCOMPLETE_REASON[ocr_result.status]
            )

        extracted_text = (ocr_result.extracted_text or "").strip()
        if ocr_result.status == OCRStatus.NO_TEXT_FOUND or not extracted_text:
            analysis.status = "completed"
            analysis.overall_result = "no_claims_detected"
            analysis.completed_at = datetime.now(timezone.utc)
            await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
            return PipelineOutcome(
                reply_text=self._response_tool.generate_no_text_found_reply(language), analysis=analysis
            )

        # --- Best-effort image/AI-generation analysis — NEVER gates classification ---
        if budget_guard.can_call_llm():
            budget_guard.record_llm_call()
            image_analysis_result = await self._deps.image_analysis_tool.analyze(image_bytes, actual_mime_type)
        else:
            image_analysis_result = ImageAnalysisResult(status=ImageAnalysisStatus.LIMIT_REACHED)

        if image_analysis_result.status == ImageAnalysisStatus.SUCCESS:
            # Only persisted when a real result exists — see
            # app/models/media_forensics_result.py's docstring for why an
            # unavailable/stub result deliberately writes no row.
            self._session.add(
                MediaForensicsResult(
                    media_attachment_id=media_attachment.id,
                    tool_name="image_analyzer",
                    provider_name=image_analysis_result.provider_name,
                    model_version=image_analysis_result.model_version,
                    ai_generated_probability=image_analysis_result.ai_generated_probability,
                    manipulation_score=image_analysis_result.manipulation_score,
                )
            )
            await self._session.flush()

        # --- Feed OCR'd text through the EXISTING Phase 2 claim pipeline ---
        extraction_tool = ClaimExtractionTool(self._deps.llm)
        try:
            budget_guard.record_llm_call()
            extraction = await extraction_tool.extract(extracted_text, language)
        except BudgetExceededError:
            return await self._finish_incomplete(analysis, budget_guard, started_at, language, "tool_call_limit")
        except LLMProviderTimeout:
            return await self._finish_incomplete(analysis, budget_guard, started_at, language, "timeout")
        except LLMProviderError:
            return await self._finish_incomplete(analysis, budget_guard, started_at, language, "provider_unavailable")

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
            verdict = self._annotate_visual_note(verdict, image_analysis_result, language)
            verdicts.append(verdict)
            await persist_claim(self._session, analysis.id, order, verdict)

        analysis.status = "completed"
        analysis.overall_result = aggregate_overall_result(verdicts)
        analysis.completed_at = datetime.now(timezone.utc)
        await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)

        return PipelineOutcome(reply_text=self._response_tool.generate(verdicts, language), analysis=analysis)

    @staticmethod
    def _annotate_visual_note(verdict: ClaimVerdict, image_analysis_result, language: str) -> ClaimVerdict:
        """Transparency note, never a classification input (agent-architecture.md's
        Multimodal Evidence Fusion policy: a missing modality is disclosed,
        not silently dropped, and never forces insufficient_evidence when
        other evidence already stands on its own)."""
        if image_analysis_result.status == ImageAnalysisStatus.SUCCESS:
            return verdict
        strings = UI_STRINGS.get(resolve_language(language), UI_STRINGS["en"])
        note = strings["visual_analysis_unavailable_note"]
        return verdict.model_copy(update={"reasoning_text": f"{verdict.reasoning_text}\n{note}"})

    async def _finish_incomplete(
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

    async def _finish_pre_analysis_error(
        self,
        analysis: Analysis,
        budget_guard: AnalysisBudgetGuard,
        started_at: datetime,
        language: str,
        error_code: str,
        reply_text: str,
    ) -> PipelineOutcome:
        """For failures before any claim investigation began (download/
        validation) — distinct from _finish_incomplete because no
        'insufficient_evidence' claim-level outcome is appropriate when no
        claim/investigation ever started."""
        analysis.status = "failed"
        analysis.error_code = error_code
        await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
        return PipelineOutcome(reply_text=reply_text, analysis=analysis)
