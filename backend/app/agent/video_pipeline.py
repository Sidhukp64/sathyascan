"""
VideoPipeline — Phase 5b's orchestrator. Mirrors AudioPipeline/ImagePipeline's
shape and reuses the same infrastructure (app/agent/claim_pipeline_shared.py,
AnalysisBudgetGuard, GlobalConcurrencyGuard, the circuit breaker) — the only
genuinely new logic is the video-specific front half.

Flow (per the user's explicit Phase 5 spec):

  media_id
    -> download (SSRF-protected, size-capped, no redirects — reuses
       MetaMediaClient, same pattern as audio's separate size-appropriate
       instance)
    -> validate actual content (magic bytes + PyAV duration/resolution
       probe — app/media/video_validation.py)
    -> SAFETY GATE (non-bypassable; BLOCKED/ERROR both stop everything)
    -> extract audio track (app/media/audio_extractor.py — real, local,
       no credentials) -> Speech-to-Text (budget-guarded, if an audio
       track exists)
    -> extract key frames (app/media/frame_extractor.py — bounded: max
       frames, sampling interval, never unlimited) -> OCR each frame
       (REUSES the exact same OCRTool from Phase 3, budget-guarded per
       frame — no second OCR engine)
    -> multimodal fusion (app/agent/multimodal_fusion.py — deterministic,
       structures spoken + on-screen content into ONE document; does NOT
       re-implement claim deduplication, that stays Claude's job)
    -> the fused document is fed through the EXISTING Phase 2 claim-
       extraction / evidence-investigation / classification pipeline
       (code-shared, not duplicated)
    -> best-effort video-forensics analysis, run INDEPENDENTLY of whether
       any claim was found — never gates classification, never described
       as "therefore false/true" (the user's explicit instruction:
       information veracity and media authenticity are two separate
       questions, never conflated in either direction)
    -> response: spoken-claim block + on-screen-text block + fact-check
       block(s) + a SEPARATE, always-present media-authenticity block

Missing-modality handling follows agent-architecture.md's Multimodal
Evidence Fusion policy exactly: if STT finds no audio track (or fails) but
frames yield on-screen text, the analysis proceeds on the on-screen text
alone — a missing modality is noted, never silently treated as "no claim
exists" as long as the OTHER modality has something to check. Only when
BOTH modalities were genuinely attempted and both found nothing does this
resolve to `no_claims_detected`; if budget ran out before EITHER modality
could even be attempted, it resolves to `insufficient_evidence` instead
(decisions.md §1A: a budget limit is never silently treated as "nothing to
check").

Cost accounting: STT and EACH frame's OCR call consume ONE unit of the SAME
AnalysisBudgetGuard.llm_tool_call_count counter every other pipeline uses —
video-forensics likewise. No second, parallel budget system. Local/
deterministic steps (download, validation, audio-track extraction, frame
sampling, multimodal-document assembly) are NOT budget-guarded, matching
Phase 3's precedent that only actual provider/LLM calls consume budget.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget_guard import AnalysisBudgetGuard, BudgetExceededError
from app.agent.claim_pipeline_shared import aggregate_overall_result, finalize_usage, investigate_claim, persist_claim
from app.agent.multimodal_fusion import FrameTextSource, build_multimodal_document
from app.agent.orchestrator import PipelineOutcome
from app.agent.retention import compute_media_retention_expiry
from app.agent.safety_gate import SafetyGate
from app.agent.schemas import ClaimVerdict
from app.agent.tools.claim_extraction import ClaimExtractionTool
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.tools.ocr import OCRStatus, OCRTool
from app.agent.tools.response_generation import ResponseGenerationTool
from app.agent.tools.source_evaluation import SourceEvaluationTool
from app.agent.tools.speech_to_text import STTStatus, STTTool
from app.agent.tools.video_forensics import VideoForensicsResult, VideoForensicsStatus, VideoForensicsTool
from app.agent.usage_tracking import is_circuit_breaker_tripped
from app.core.concurrency import ConcurrencyLimitReachedError, GlobalConcurrencyGuard
from app.core.config import Settings
from app.core.logging import log_event
from app.integrations.claude_client import LLMClient, LLMProviderError, LLMProviderTimeout
from app.media.audio_extractor import AudioExtractionError, extract_audio_track
from app.media.audio_validation import validate_audio
from app.media.downloader import (
    MediaDownloadError,
    MediaDownloadTimeout,
    MediaTooLargeError,
    MetaMediaClient,
    UnsafeMediaURLError,
)
from app.media.frame_extractor import FrameExtractionError, extract_frames
from app.media.validation import MediaValidationError
from app.media.video_validation import validate_video
from app.models.analysis import Analysis
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.safety_gate_event import SafetyGateEvent
from app.models.transcript import Transcript
from app.models.user import User
from app.models.video_frame import VideoFrame

logger = logging.getLogger(__name__)

_FORENSICS_ANALYZED_STATUSES = {
    VideoForensicsStatus.AI_GENERATED_LIKELY,
    VideoForensicsStatus.AI_GENERATED_UNLIKELY,
    VideoForensicsStatus.MANIPULATED_LIKELY,
    VideoForensicsStatus.AUTHENTICITY_UNCERTAIN,
}


@dataclass
class VideoPipelineDependencies:
    llm: LLMClient
    search_provider: object  # EvidenceSearchProvider — loosely typed, matches PipelineDependencies
    redis: object
    media_client: MetaMediaClient
    safety_gate: SafetyGate
    stt_tool: STTTool
    ocr_tool: OCRTool
    video_forensics_tool: VideoForensicsTool


class VideoPipeline:
    def __init__(self, session: AsyncSession, deps: VideoPipelineDependencies, settings: Settings) -> None:
        self._session = session
        self._deps = deps
        self._settings = settings
        self._response_tool = ResponseGenerationTool()

    async def run(self, *, user: User, media_id: str, source_wamid: str, language: str) -> PipelineOutcome:
        # Duplicate-content reuse for video is out of Phase 5 scope, same
        # scope decision Phase 3 made for images — every video is freshly
        # analyzed.

        if await is_circuit_breaker_tripped(self._session, self._settings.daily_spend_circuit_breaker_usd):
            log_event(logger, logging.WARNING, "daily circuit breaker tripped, declining video analysis")
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
            log_event(logger, logging.WARNING, "global concurrency limit reached, declining video analysis")
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
            input_type="video",
            input_text="",  # transcript/on-screen text live in transcripts/video_frames, not duplicated here
            content_fingerprint=hashlib.sha256(media_id.encode()).hexdigest(),  # updated once real bytes exist
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
            video_bytes, _claimed_mime_type = await self._deps.media_client.download_media(media_id)
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
            log_event(logger, logging.WARNING, "video download blocked (unsafe URL/SSRF/redirect)")
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "unsafe_url",
                self._response_tool.generate_processing_error_reply(language),
            )
        except MediaDownloadError:
            return await self._finish_pre_analysis_error(
                analysis, budget_guard, started_at, language, "download_failed",
                self._response_tool.generate_processing_error_reply(language),
            )

        analysis.content_fingerprint = hashlib.sha256(video_bytes).hexdigest()

        # --- Validate actual content (magic bytes + PyAV duration/resolution probe) ---
        try:
            actual_mime_type, _duration, _width, _height = validate_video(
                video_bytes, self._settings.max_video_duration_seconds, self._settings.max_image_pixels
            )
        except MediaValidationError as exc:
            if exc.reason == "duration_too_long":
                reply = self._response_tool.generate_video_duration_too_long_reply(language)
            elif exc.reason in ("unsupported_format", "resolution_too_large"):
                reply = self._response_tool.generate_unsupported_media_reply(language)
            else:
                reply = self._response_tool.generate_processing_error_reply(language)
            return await self._finish_pre_analysis_error(analysis, budget_guard, started_at, language, exc.reason, reply)

        media_attachment = MediaAttachment(
            analysis_id=analysis.id,
            whatsapp_media_id=media_id,
            media_type="video",
            mime_type=actual_mime_type,
            file_size_bytes=len(video_bytes),
            sha256_hash=bytes.fromhex(analysis.content_fingerprint),
            # Phase 6 — Privacy Mode retention, see image_pipeline.py's
            # identical comment for the rationale.
            retention_expires_at=compute_media_retention_expiry(
                analysis.created_at, analysis.privacy_mode_snapshot, self._settings
            ),
        )
        self._session.add(media_attachment)
        await self._session.flush()

        # --- SAFETY GATE — non-bypassable, before any analysis touches the video ---
        safety_result = await self._deps.safety_gate.check(video_bytes, actual_mime_type)
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
            return PipelineOutcome(
                reply_text=self._response_tool.generate_content_declined_reply(language), analysis=analysis
            )

        # --- Audio track -> Speech-to-Text (budget-guarded; missing/failed audio is NOT fatal) ---
        transcript_text, transcript_result = await self._transcribe_audio_track(
            video_bytes, budget_guard, media_attachment.id, language
        )

        # --- Key-frame extraction -> OCR per frame (budget-guarded per frame; REUSES OCRTool) ---
        frame_sources, any_frame_attempted = await self._extract_and_ocr_frames(
            video_bytes, budget_guard, media_attachment.id, language
        )

        # --- Best-effort video forensics — NEVER gates classification, ALWAYS attempted ---
        if budget_guard.can_call_llm():
            budget_guard.record_llm_call()
            forensics_result = await self._deps.video_forensics_tool.analyze(video_bytes, actual_mime_type)
        else:
            forensics_result = VideoForensicsResult(status=VideoForensicsStatus.PROVIDER_UNAVAILABLE)

        if forensics_result.status in _FORENSICS_ANALYZED_STATUSES:
            self._session.add(
                MediaForensicsResult(
                    media_attachment_id=media_attachment.id,
                    tool_name="video_deepfake",
                    provider_name=forensics_result.provider_name or "unknown",
                    model_version=forensics_result.model_version or "unknown",
                    ai_generated_probability=forensics_result.ai_generated_probability,
                    manipulation_score=forensics_result.manipulation_score,
                )
            )
            await self._session.flush()

        on_screen_text_summary = "; ".join(
            dict.fromkeys(s.extracted_text for s in frame_sources)  # de-ordered-dedupe for the display summary
        )

        # --- Nothing attempted at all (budget exhausted before either modality ran) ---
        transcript_attempted = transcript_result is not None
        if not transcript_attempted and not any_frame_attempted:
            return await self._finish_incomplete(analysis, budget_guard, started_at, language, "tool_call_limit")

        document = build_multimodal_document(transcript_text, frame_sources)

        if not document:
            # Both modalities were genuinely attempted and both found nothing
            # — a completed investigation with nothing to check, distinct
            # from the budget-exhausted case above (decisions.md §1A).
            analysis.status = "completed"
            analysis.overall_result = "no_claims_detected"
            analysis.completed_at = datetime.now(timezone.utc)
            await finalize_usage(self._session, self._settings, analysis, budget_guard, started_at)
            return PipelineOutcome(
                reply_text=self._response_tool.generate_video_analysis_reply(
                    None, None, [], forensics_result, language
                ),
                analysis=analysis,
            )

        # --- Feed the fused document through the EXISTING Phase 2 claim pipeline ---
        extraction_tool = ClaimExtractionTool(self._deps.llm)
        try:
            budget_guard.record_llm_call()
            extraction = await extraction_tool.extract(document, language)
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
                reply_text=self._response_tool.generate_video_analysis_reply(
                    transcript_text, on_screen_text_summary or None, [], forensics_result, language
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
            reply_text=self._response_tool.generate_video_analysis_reply(
                transcript_text, on_screen_text_summary or None, verdicts, forensics_result, language
            ),
            analysis=analysis,
        )

    async def _transcribe_audio_track(
        self, video_bytes: bytes, budget_guard: AnalysisBudgetGuard, media_attachment_id, language: str
    ) -> tuple[str | None, object | None]:
        """Returns (transcript_text_or_None, stt_result_or_None).
        stt_result is None only when NO audio track exists at all or STT was
        never attempted (budget exhausted) — a genuinely-run STT call that
        found nothing still returns a non-None result, distinguishing
        "not attempted" from "attempted, found nothing"."""
        try:
            audio_wav_bytes = extract_audio_track(video_bytes)
        except AudioExtractionError:
            log_event(logger, logging.WARNING, "video audio-track extraction failed, continuing without transcript")
            return None, None

        if audio_wav_bytes is None:
            return None, None  # no audio stream in this video — not an error

        try:
            validate_audio(audio_wav_bytes, self._settings.max_audio_duration_seconds)
        except MediaValidationError:
            return None, None

        if not budget_guard.can_call_llm():
            return None, None

        budget_guard.record_llm_call()
        stt_result = await self._deps.stt_tool.transcribe(audio_wav_bytes, "audio/wav", language_hint=language)

        if stt_result.status != STTStatus.SUCCESS:
            return None, stt_result

        transcript_text = (stt_result.transcript or "").strip()
        if transcript_text:
            self._session.add(
                Transcript(
                    media_attachment_id=media_attachment_id,
                    language=stt_result.detected_language,
                    text=transcript_text,
                    confidence=stt_result.confidence,
                    engine=stt_result.provider_name,
                )
            )
            await self._session.flush()

        return (transcript_text or None), stt_result

    async def _extract_and_ocr_frames(
        self, video_bytes: bytes, budget_guard: AnalysisBudgetGuard, media_attachment_id, language: str
    ) -> tuple[list[FrameTextSource], bool]:
        """Returns (frame_text_sources, any_frame_attempted). Stops calling
        OCR once budget runs out mid-loop rather than aborting the whole
        analysis — whatever frames were already OCR'd remain usable."""
        try:
            frames = extract_frames(
                video_bytes,
                max_frames=self._settings.max_video_frames,
                sampling_interval_seconds=self._settings.video_frame_sampling_interval_seconds,
            )
        except FrameExtractionError:
            log_event(logger, logging.WARNING, "video frame extraction failed, continuing without on-screen text")
            return [], False

        sources: list[FrameTextSource] = []
        any_attempted = False

        for frame in frames:
            if not budget_guard.can_call_llm():
                break
            budget_guard.record_llm_call()
            any_attempted = True

            ocr_result = await self._deps.ocr_tool.extract(frame.jpeg_bytes, "image/jpeg", language_hint=language)

            self._session.add(
                VideoFrame(
                    media_attachment_id=media_attachment_id,
                    frame_timestamp_ms=int(frame.timestamp_seconds * 1000),
                    extracted_text=ocr_result.extracted_text if ocr_result.status == OCRStatus.SUCCESS else None,
                    detected_language=ocr_result.detected_language,
                    ocr_confidence=ocr_result.confidence,
                )
            )

            if ocr_result.status == OCRStatus.SUCCESS and (ocr_result.extracted_text or "").strip():
                sources.append(
                    FrameTextSource(
                        timestamp_seconds=frame.timestamp_seconds, extracted_text=ocr_result.extracted_text.strip()
                    )
                )

        if any_attempted:
            await self._session.flush()

        return sources, any_attempted

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
