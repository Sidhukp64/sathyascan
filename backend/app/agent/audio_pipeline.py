"""
AudioPipeline — Phase 5a's orchestrator. Mirrors ImagePipeline's shape and
reuses its infrastructure directly (app/agent/claim_pipeline_shared.py,
AnalysisBudgetGuard, GlobalConcurrencyGuard, the circuit breaker) rather than
duplicating it — the only genuinely new logic here is the audio-specific
front half (download, validate, safety gate, transcribe).

Flow:

  media_id
    -> download (SSRF-protected, size-capped, no redirects — reuses the
       EXACT SAME MetaMediaClient Phase 3 built; it was already
       media-type-agnostic)
    -> validate actual content (magic bytes + PyAV duration probe —
       app/media/audio_validation.py)
    -> SAFETY GATE (non-bypassable; BLOCKED/ERROR both stop everything,
       exactly like ImagePipeline)
    -> Speech-to-Text (budget-guarded, reuses the SAME counter/limit as
       text/image/OCR calls)
    -> [no speech / empty transcript -> friendly reply, done]
    -> transcript fed through the EXISTING Phase 2 claim-extraction /
       evidence-investigation / classification pipeline (code-shared, not
       duplicated) — the transcript is NEVER translated before this step
       (decisions.md §3's "translate for search, not for storage/display"
       policy: the transcript stays in its original spoken language; only
       evidence search translates internally if a provider needs it)
    -> best-effort audio-forensics analysis, run INDEPENDENTLY of whether
       any claim was found (never gates classification, never described as
       "therefore false/true" — see app/agent/tools/audio_forensics.py's
       docstring and the user's explicit Phase 5 instruction: information
       veracity and media authenticity are two separate questions, never
       conflated in either direction)
    -> response: fact-check block(s) + a SEPARATE, always-present media-
       authenticity block

Cost accounting: STT and audio-forensics calls each consume ONE unit of the
SAME AnalysisBudgetGuard.llm_tool_call_count counter every other pipeline
uses — no second, parallel budget system.

Unlike ImagePipeline's OCR'd text (never persisted anywhere), the
transcript IS durably persisted to the `transcripts` table (Phase 5's
newly-migrated table, already designed pre-Phase-5) — the user's explicit
instruction to "keep the original transcript available for auditability."
`analyses.input_text` stays empty for audio, same reasoning as image
(`analyses.input_text=""`) — the real content lives in one place
(`transcripts`), not duplicated into two columns.
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
from app.agent.tools.audio_forensics import AudioForensicsResult, AudioForensicsStatus, AudioForensicsTool
from app.agent.tools.claim_extraction import ClaimExtractionTool
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.tools.response_generation import ResponseGenerationTool
from app.agent.tools.source_evaluation import SourceEvaluationTool
from app.agent.tools.speech_to_text import STTStatus, STTTool
from app.agent.usage_tracking import is_circuit_breaker_tripped
from app.core.concurrency import ConcurrencyLimitReachedError, GlobalConcurrencyGuard
from app.core.config import Settings
from app.core.logging import log_event
from app.integrations.claude_client import LLMClient, LLMProviderError, LLMProviderTimeout
from app.media.audio_validation import validate_audio
from app.media.downloader import (
    MediaDownloadError,
    MediaDownloadTimeout,
    MediaTooLargeError,
    MetaMediaClient,
    UnsafeMediaURLError,
)
from app.media.validation import MediaValidationError
from app.models.analysis import Analysis
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.safety_gate_event import SafetyGateEvent
from app.models.transcript import Transcript
from app.models.user import User

logger = logging.getLogger(__name__)

# incomplete_reason values, per decisions.md §1A: search_limit | tool_call_limit |
# provider_unavailable | timeout | infra_error | other
_STT_STATUS_TO_INCOMPLETE_REASON = {
    STTStatus.PROVIDER_UNAVAILABLE: "provider_unavailable",
    STTStatus.FAILED: "infra_error",
    STTStatus.LIMIT_REACHED: "tool_call_limit",
}

# Forensics statuses that represent a genuine analyzed result (worth
# persisting a MediaForensicsResult row for) — mirrors ImagePipeline's exact
# "absence of a row = no analysis attempted" precedent.
_FORENSICS_ANALYZED_STATUSES = {
    AudioForensicsStatus.AI_GENERATED_LIKELY,
    AudioForensicsStatus.AI_GENERATED_UNLIKELY,
    AudioForensicsStatus.MANIPULATED_LIKELY,
    AudioForensicsStatus.AUTHENTICITY_UNCERTAIN,
}


@dataclass
class AudioPipelineDependencies:
    llm: LLMClient
    search_provider: object  # EvidenceSearchProvider — loosely typed, matches PipelineDependencies
    redis: object
    media_client: MetaMediaClient
    safety_gate: SafetyGate
    stt_tool: STTTool
    audio_forensics_tool: AudioForensicsTool


class AudioPipeline:
    def __init__(self, session: AsyncSession, deps: AudioPipelineDependencies, settings: Settings) -> None:
        self._session = session
        self._deps = deps
        self._settings = settings
        self._response_tool = ResponseGenerationTool()

    async def run(self, *, user: User, media_id: str, source_wamid: str, language: str) -> PipelineOutcome:
        # Duplicate-content reuse for audio is out of Phase 5 scope, same
        # scope decision Phase 3 made for images — every voice note/audio
        # file is freshly analyzed.

        if await is_circuit_breaker_tripped(self._session, self._settings.daily_spend_circuit_breaker_usd):
            log_event(logger, logging.WARNING, "daily circuit breaker tripped, declining audio analysis")
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
            log_event(logger, logging.WARNING, "global concurrency limit reached, declining audio analysis")
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

        analysis = Analysis(
            user_id=user.id,
            input_type="audio",
            input_text="",  # transcript lives in `transcripts`, not duplicated here — see module docstring
            content_fingerprint=hashlib.sha256(media_id.encode()).hexdigest(),  # updated below once real bytes exist
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
            audio_bytes, _claimed_mime_type = await self._deps.media_client.download_media(media_id)
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
            log_event(logger, logging.WARNING, "audio download blocked (unsafe URL/SSRF/redirect)")
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "unsafe_url",
                self._response_tool.generate_processing_error_reply(language),
            )
        except MediaDownloadError:
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "download_failed",
                self._response_tool.generate_processing_error_reply(language),
            )

        analysis.content_fingerprint = hashlib.sha256(audio_bytes).hexdigest()

        # --- Validate actual content (magic bytes + PyAV duration probe) ---
        try:
            actual_mime_type, _duration_seconds = validate_audio(audio_bytes, self._settings.max_audio_duration_seconds)
        except MediaValidationError as exc:
            if exc.reason == "duration_too_long":
                reply = self._response_tool.generate_audio_duration_too_long_reply(language)
            elif exc.reason == "unsupported_format":
                reply = self._response_tool.generate_unsupported_media_reply(language)
            else:
                reply = self._response_tool.generate_processing_error_reply(language)
            return await self._finish_pre_analysis_error(analysis, budget_guard, started_at, language, exc.reason, reply)

        media_attachment = MediaAttachment(
            analysis_id=analysis.id,
            whatsapp_media_id=media_id,
            media_type="audio",
            mime_type=actual_mime_type,  # the real sniffed type — never the claimed one
            file_size_bytes=len(audio_bytes),
            sha256_hash=bytes.fromhex(analysis.content_fingerprint),
            # Phase 6 — Privacy Mode retention, see image_pipeline.py's
            # identical comment for the rationale.
            retention_expires_at=compute_media_retention_expiry(
                analysis.created_at, analysis.privacy_mode_snapshot, self._settings
            ),
        )
        self._session.add(media_attachment)
        await self._session.flush()

        # --- SAFETY GATE — non-bypassable, before any analysis touches the audio ---
        safety_result = await self._deps.safety_gate.check(audio_bytes, actual_mime_type)
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

        # --- Speech-to-Text (budget-guarded, same counter/limit as text claims) ---
        try:
            budget_guard.record_llm_call()
        except BudgetExceededError:
            return await self._finish_incomplete(analysis, budget_guard, started_at, language, "tool_call_limit")

        stt_result = await self._deps.stt_tool.transcribe(audio_bytes, actual_mime_type, language_hint=language)

        if stt_result.status in _STT_STATUS_TO_INCOMPLETE_REASON:
            # Per the user's explicit Phase 5 spec: transcription failure ->
            # a dedicated reply, distinct from the generic processing error,
            # explicitly showing insufficient_evidence — never a guess.
            analysis.status = "failed"
            analysis.error_code = _STT_STATUS_TO_INCOMPLETE_REASON[stt_result.status]
            analysis.overall_result = "insufficient_evidence"
            await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
            return PipelineOutcome(
                reply_text=self._response_tool.generate_transcription_failed_reply(language), analysis=analysis
            )

        transcript_text = (stt_result.transcript or "").strip()
        no_speech_found = stt_result.status == STTStatus.NO_SPEECH_FOUND or not transcript_text

        if stt_result.status == STTStatus.SUCCESS and transcript_text:
            self._session.add(
                Transcript(
                    media_attachment_id=media_attachment.id,
                    language=stt_result.detected_language,
                    text=transcript_text,
                    confidence=stt_result.confidence,
                    engine=stt_result.provider_name,
                )
            )
            await self._session.flush()

        # --- Best-effort audio forensics — NEVER gates classification, ALWAYS attempted ---
        if budget_guard.can_call_llm():
            budget_guard.record_llm_call()
            forensics_result = await self._deps.audio_forensics_tool.analyze(audio_bytes, actual_mime_type)
        else:
            forensics_result = AudioForensicsResult(status=AudioForensicsStatus.PROVIDER_UNAVAILABLE)

        if forensics_result.status in _FORENSICS_ANALYZED_STATUSES:
            self._session.add(
                MediaForensicsResult(
                    media_attachment_id=media_attachment.id,
                    tool_name="audio_voice_detect",
                    provider_name=forensics_result.provider_name or "unknown",
                    model_version=forensics_result.model_version or "unknown",
                    ai_generated_probability=forensics_result.ai_generated_probability,
                    manipulation_score=forensics_result.manipulation_score,
                )
            )
            await self._session.flush()

        if no_speech_found:
            analysis.status = "completed"
            analysis.overall_result = "no_claims_detected"
            analysis.completed_at = datetime.now(timezone.utc)
            await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
            return PipelineOutcome(
                reply_text=self._response_tool.generate_audio_analysis_reply(
                    transcript_text or None, [], forensics_result, language, no_speech_found=True
                ),
                analysis=analysis,
            )

        # --- Feed transcript through the EXISTING Phase 2 claim pipeline ---
        # (NEVER translated first — decisions.md §3's translate-for-search-
        # only policy; the transcript stays in its original spoken language.)
        extraction_tool = ClaimExtractionTool(self._deps.llm)
        try:
            budget_guard.record_llm_call()
            extraction = await extraction_tool.extract(transcript_text, language)
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
                reply_text=self._response_tool.generate_audio_analysis_reply(
                    transcript_text, [], forensics_result, language
                ),
                analysis=analysis,
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

        return PipelineOutcome(
            reply_text=self._response_tool.generate_audio_analysis_reply(
                transcript_text, verdicts, forensics_result, language
            ),
            analysis=analysis,
        )

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
