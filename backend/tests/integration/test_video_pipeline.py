"""
Full video-pipeline scenario tests, exercised directly against VideoPipeline
(mirrors tests/integration/test_audio_pipeline.py's structure) so each
scenario can script the fakes precisely. Uses a real in-memory SQLite DB and
REAL PyAV-encoded video fixtures (make_sample_mp4_bytes) so persistence and
actual frame/audio-track extraction are tested against real logic, not mocks.
"""

import fakeredis.aioredis
from sqlalchemy import select

from app.agent.safety_gate import SafetyCheckResult, SafetyGate, SafetyOutcome
from app.agent.tools.ocr import OCRResult, OCRStatus, OCRTool
from app.agent.tools.speech_to_text import STTResult, STTStatus, STTTool
from app.agent.tools.video_forensics import VideoForensicsResult, VideoForensicsStatus, VideoForensicsTool
from app.agent.user_service import get_or_create_user
from app.agent.video_pipeline import VideoPipeline, VideoPipelineDependencies
from app.core.config import Settings
from app.integrations.evidence_search_client import SearchResult
from app.media.downloader import MediaTooLargeError, UnsafeMediaURLError
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.safety_gate_event import SafetyGateEvent
from app.models.transcript import Transcript
from app.models.video_frame import VideoFrame

from tests.conftest import (
    TEST_PHONE_ENCRYPTION_KEY,
    TEST_PHONE_PEPPER,
    FakeEvidenceSearchProvider,
    FakeLLMClient,
    FakeMediaClient,
    make_sample_mp4_bytes,
    make_text_response,
    make_tool_response,
)


def _settings(**overrides) -> Settings:
    base = dict(
        WHATSAPP_APP_SECRET="x",
        WHATSAPP_WEBHOOK_VERIFY_TOKEN="x",
        WHATSAPP_ACCESS_TOKEN="x",
        WHATSAPP_PHONE_NUMBER_ID="x",
        PHONE_HASH_PEPPER=TEST_PHONE_PEPPER,
        PHONE_ENCRYPTION_KEY=TEST_PHONE_ENCRYPTION_KEY,
        MAX_EVIDENCE_SEARCHES_PER_ANALYSIS=5,
        MAX_LLM_TOOL_CALLS_PER_ANALYSIS=20,
        DAILY_SPEND_CIRCUIT_BREAKER_USD=50.0,
        GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT=20,
        MIN_CONCURRING_TIER2_SOURCES=2,
        MAX_AUDIO_DURATION_SECONDS=600.0,
        MAX_VIDEO_DURATION_SECONDS=300.0,
        MAX_VIDEO_FRAMES=8,
        VIDEO_FRAME_SAMPLING_INTERVAL_SECONDS=1.0,
        MAX_IMAGE_PIXELS=40_000_000,
        MAX_UPLOAD_SIZE_BYTES_VIDEO=104_857_600,
    )
    base.update(overrides)
    return Settings(**base)


async def _make_user(db_session, phone="15551234567"):
    return await get_or_create_user(db_session, phone, TEST_PHONE_PEPPER, TEST_PHONE_ENCRYPTION_KEY)


class _TurnScript:
    def __init__(self, claims_response, synthesis_response=None, investigation_turns=None):
        self.claims_response = claims_response
        self.synthesis_response = synthesis_response
        self.investigation_turns = list(investigation_turns or [])
        self._i = 0

    def __call__(self, ctx):
        if ctx.forced_tool_name == "record_claims":
            return self.claims_response
        if ctx.forced_tool_name == "record_synthesis":
            return self.synthesis_response
        if self._i < len(self.investigation_turns):
            resp = self.investigation_turns[self._i]
            self._i += 1
            return resp
        return make_text_response()


def _pipeline(db_session, settings, llm, search, media_client, safety_provider, stt_provider, ocr_provider, forensics_provider) -> VideoPipeline:
    deps = VideoPipelineDependencies(
        llm=llm,
        search_provider=search,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        media_client=media_client,
        safety_gate=SafetyGate(safety_provider),
        stt_tool=STTTool(stt_provider),
        ocr_tool=OCRTool(ocr_provider),
        video_forensics_tool=VideoForensicsTool(forensics_provider),
    )
    return VideoPipeline(db_session, deps, settings)


def _passed_safety_provider():
    class _P:
        async def check(self, media_bytes, mime_type):
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    return _P()


def _blocked_safety_provider():
    class _P:
        async def check(self, media_bytes, mime_type):
            return SafetyCheckResult(outcome=SafetyOutcome.BLOCKED, check_type="content_classifier", provider_name="fake")

    return _P()


def _stt_provider_returning(result: STTResult):
    class _P:
        async def transcribe(self, audio_bytes, mime_type, language_hint):
            return result

    return _P()


def _unavailable_stt_provider():
    return _stt_provider_returning(STTResult(status=STTStatus.PROVIDER_UNAVAILABLE))


def _ocr_provider_returning(result: OCRResult):
    class _P:
        async def extract_text(self, image_bytes, mime_type, language_hint):
            return result

    return _P()


def _unavailable_ocr_provider():
    return _ocr_provider_returning(OCRResult(status=OCRStatus.PROVIDER_UNAVAILABLE))


def _no_text_ocr_provider():
    return _ocr_provider_returning(OCRResult(status=OCRStatus.NO_TEXT_FOUND))


def _unavailable_forensics_provider():
    class _P:
        async def analyze(self, video_bytes, mime_type):
            return VideoForensicsResult(status=VideoForensicsStatus.PROVIDER_UNAVAILABLE)

    return _P()


def _forensics_provider_returning(result: VideoForensicsResult):
    class _P:
        async def analyze(self, video_bytes, mime_type):
            return result

    return _P()


async def test_full_pipeline_spoken_and_on_screen_claims_combined(db_session, seeded_registry):
    llm = FakeLLMClient()
    llm.responder = _TurnScript(
        claims_response=make_tool_response(
            "record_claims", {"claims": [{"text": "The scheme was announced.", "category": "gov_scheme"}]}
        ),
        synthesis_response=make_tool_response(
            "record_synthesis",
            {"evidence_stances": ["supporting"], "suggested_result": "verified", "claim_confidence": 0.9, "reasoning_text": "Confirmed by an official source."},
        ),
        investigation_turns=[make_tool_response("search_evidence", {"query": "scheme announcement"})],
    )
    search = FakeEvidenceSearchProvider(
        responder=lambda q: [SearchResult(title="Official notice", url="https://pib.gov.in/notice", snippet="Confirmed.")]
    )
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=3), "video/mp4"))
    stt_provider = _stt_provider_returning(
        STTResult(status=STTStatus.SUCCESS, transcript="The scheme was announced.", detected_language="en", confidence=0.9, provider_name="fake_stt")
    )
    ocr_provider = _ocr_provider_returning(
        OCRResult(status=OCRStatus.SUCCESS, extracted_text="Rs 50,000 Student Scheme", provider_name="fake_ocr")
    )

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), stt_provider, ocr_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-1", source_wamid="wamid.v1", language="en")

    assert outcome.analysis.status == "completed"
    assert outcome.analysis.overall_result == "verified"
    assert outcome.analysis.input_type == "video"
    assert outcome.analysis.input_text == ""
    assert "SathyaScan Video Result" in outcome.reply_text
    assert "The scheme was announced." in outcome.reply_text
    assert "Rs 50,000 Student Scheme" in outcome.reply_text
    assert "AI-generation detection unavailable" in outcome.reply_text
    assert "independent results" in outcome.reply_text

    transcript_result = await db_session.execute(select(Transcript))
    transcripts = transcript_result.scalars().all()
    assert len(transcripts) == 1
    assert transcripts[0].text == "The scheme was announced."

    frames_result = await db_session.execute(select(VideoFrame))
    frames = frames_result.scalars().all()
    assert len(frames) >= 1
    assert any(f.extracted_text == "Rs 50,000 Student Scheme" for f in frames)

    media_result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.analysis_id == outcome.analysis.id))
    media_attachment = media_result.scalars().one()
    assert media_attachment.media_type == "video"
    assert media_attachment.storage_path is None  # no blob storage, same as audio/images


async def test_on_screen_text_alone_suffices_when_audio_is_missing(db_session, seeded_registry):
    """agent-architecture.md's Multimodal Evidence Fusion policy: a missing
    modality doesn't force insufficient_evidence when the OTHER modality
    already provides enough to check."""
    llm = FakeLLMClient()
    llm.responder = _TurnScript(
        claims_response=make_tool_response(
            "record_claims", {"claims": [{"text": "Rs 50,000 Student Scheme", "category": "gov_scheme"}]}
        ),
        synthesis_response=make_tool_response(
            "record_synthesis",
            {"evidence_stances": [], "suggested_result": "unverified", "claim_confidence": 0.2, "reasoning_text": "No evidence found."},
        ),
        investigation_turns=[],
    )
    search = FakeEvidenceSearchProvider(responder=lambda q: [])
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2, with_audio=False), "video/mp4"))
    ocr_provider = _ocr_provider_returning(
        OCRResult(status=OCRStatus.SUCCESS, extracted_text="Rs 50,000 Student Scheme", provider_name="fake_ocr")
    )

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), _unavailable_stt_provider(), ocr_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-2", source_wamid="wamid.v2", language="en")

    assert outcome.analysis.overall_result == "unverified"  # NOT insufficient_evidence
    assert "Rs 50,000 Student Scheme" in outcome.reply_text

    transcript_result = await db_session.execute(select(Transcript))
    assert transcript_result.scalars().all() == []  # no audio track — nothing to persist


async def test_unsafe_video_is_stopped_before_any_analysis(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(), "video/mp4"))
    stt_provider = _stt_provider_returning(STTResult(status=STTStatus.SUCCESS, transcript="should never be read"))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _blocked_safety_provider(), stt_provider, _unavailable_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-3", source_wamid="wamid.v3", language="en")

    assert outcome.analysis.status == "blocked"
    assert llm.call_log == []

    result = await db_session.execute(select(SafetyGateEvent).where(SafetyGateEvent.analysis_id == outcome.analysis.id))
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].outcome == "blocked"

    transcript_result = await db_session.execute(select(Transcript))
    assert transcript_result.scalars().all() == []
    frames_result = await db_session.execute(select(VideoFrame))
    assert frames_result.scalars().all() == []


async def test_no_meaningful_claim_in_either_modality_yields_no_claims_detected(db_session, seeded_registry):
    """The user's exact example: 'I couldn't find a specific factual claim
    to verify in this video.' — both modalities genuinely attempted, both
    found nothing."""
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4"))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _unavailable_stt_provider(), _no_text_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-4", source_wamid="wamid.v4", language="en")

    assert outcome.analysis.overall_result == "no_claims_detected"
    assert outcome.analysis.status == "completed"
    assert "couldn't find a specific factual claim" in outcome.reply_text
    assert llm.call_log == []  # ClaimExtractionTool never even called — empty document short-circuits


async def test_budget_exhausted_before_either_modality_yields_insufficient_evidence(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4"))
    stt_calls: list[str] = []
    ocr_calls: list[str] = []

    class _CountingSTT:
        async def transcribe(self, audio_bytes, mime_type, language_hint):
            stt_calls.append("called")
            return STTResult(status=STTStatus.SUCCESS, transcript="x")

    class _CountingOCR:
        async def extract_text(self, image_bytes, mime_type, language_hint):
            ocr_calls.append("called")
            return OCRResult(status=OCRStatus.SUCCESS, extracted_text="x")

    user = await _make_user(db_session)
    settings = _settings(MAX_LLM_TOOL_CALLS_PER_ANALYSIS=0)
    pipeline = _pipeline(db_session, settings, llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _CountingSTT(), _CountingOCR(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-5", source_wamid="wamid.v5", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "tool_call_limit"
    assert stt_calls == []
    assert ocr_calls == []


async def test_partial_budget_uses_whatever_frames_were_ocrd_before_running_out(db_session, seeded_registry):
    """Budget runs out mid-frame-loop — whatever was already OCR'd remains
    usable, the whole analysis doesn't abort."""
    llm = FakeLLMClient()
    llm.responder = _TurnScript(
        claims_response=make_tool_response("record_claims", {"claims": [{"text": "Some Text", "category": "other"}]}),
        synthesis_response=make_tool_response(
            "record_synthesis",
            {"evidence_stances": [], "suggested_result": "unverified", "claim_confidence": 0.2, "reasoning_text": "No evidence found."},
        ),
        investigation_turns=[],
    )
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=5, with_audio=False), "video/mp4"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS, extracted_text="Some Text"))

    user = await _make_user(db_session)
    # Budget for exactly ONE OCR call plus the extraction call plus a
    # cushion — not enough to OCR every sampled frame, forcing the
    # mid-loop-stop path.
    settings = _settings(MAX_LLM_TOOL_CALLS_PER_ANALYSIS=2, MAX_VIDEO_FRAMES=8, VIDEO_FRAME_SAMPLING_INTERVAL_SECONDS=1.0)
    pipeline = _pipeline(db_session, settings, llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _unavailable_stt_provider(), ocr_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-6", source_wamid="wamid.v6", language="en")

    frames_result = await db_session.execute(select(VideoFrame))
    frames = frames_result.scalars().all()
    assert 0 < len(frames) < 5  # stopped early, not all candidate frames processed


async def test_oversized_video_is_a_friendly_pre_analysis_error(db_session, seeded_registry):
    llm = FakeLLMClient()

    def _raise(mid):
        raise MediaTooLargeError("too big")

    media_client = FakeMediaClient(responder=_raise)

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _unavailable_stt_provider(), _unavailable_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-7", source_wamid="wamid.v7", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "file_too_large"
    assert outcome.analysis.overall_result is None
    assert llm.call_log == []


async def test_unsafe_media_url_is_a_friendly_pre_analysis_error(db_session, seeded_registry):
    llm = FakeLLMClient()

    def _raise(mid):
        raise UnsafeMediaURLError("blocked")

    media_client = FakeMediaClient(responder=_raise)

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _unavailable_stt_provider(), _unavailable_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-8", source_wamid="wamid.v8", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "unsafe_url"


async def test_duration_too_long_is_rejected_before_safety_gate(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=5), "video/mp4"))
    safety_provider_called = []

    class _TrackedSafety:
        async def check(self, media_bytes, mime_type):
            safety_provider_called.append(True)
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    user = await _make_user(db_session)
    settings = _settings(MAX_VIDEO_DURATION_SECONDS=1.0)
    pipeline = _pipeline(db_session, settings, llm, FakeEvidenceSearchProvider(), media_client, _TrackedSafety(), _unavailable_stt_provider(), _unavailable_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-9", source_wamid="wamid.v9", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "duration_too_long"
    assert safety_provider_called == []
    assert "too long" in outcome.reply_text.lower()


async def test_unsupported_format_is_rejected_after_download_before_safety_gate(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (b"not real video bytes", "video/mp4"))
    safety_provider_called = []

    class _TrackedSafety:
        async def check(self, media_bytes, mime_type):
            safety_provider_called.append(True)
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _TrackedSafety(), _unavailable_stt_provider(), _unavailable_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-10", source_wamid="wamid.v10", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "unsupported_format"
    assert safety_provider_called == []


async def test_no_video_bytes_are_ever_persisted(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4"))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _unavailable_stt_provider(), _no_text_ocr_provider(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="video-11", source_wamid="wamid.v11", language="en")

    media_result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.analysis_id == outcome.analysis.id))
    attachment = media_result.scalars().one()
    assert attachment.storage_path is None


class TestVideoForensicsResultHandling:
    async def test_forensics_unavailable_is_disclosed_never_gates_classification(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        llm.responder = _TurnScript(
            claims_response=make_tool_response("record_claims", {"claims": [{"text": "The scheme was announced.", "category": "gov_scheme"}]}),
            synthesis_response=make_tool_response(
                "record_synthesis",
                {"evidence_stances": ["supporting"], "suggested_result": "verified", "claim_confidence": 0.9, "reasoning_text": "Confirmed."},
            ),
            investigation_turns=[make_tool_response("search_evidence", {"query": "scheme"})],
        )
        search = FakeEvidenceSearchProvider(responder=lambda q: [SearchResult(title="Official", url="https://pib.gov.in/x", snippet="Confirmed.")])
        media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4"))
        stt_provider = _stt_provider_returning(STTResult(status=STTStatus.SUCCESS, transcript="The scheme was announced.", detected_language="en"))

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), stt_provider, _no_text_ocr_provider(), _unavailable_forensics_provider())
        outcome = await pipeline.run(user=user, media_id="video-12", source_wamid="wamid.v12", language="en")

        assert outcome.analysis.overall_result == "verified"
        assert "AI-generation detection unavailable" in outcome.reply_text

        forensics_result = await db_session.execute(select(MediaForensicsResult))
        assert forensics_result.scalars().all() == []

    async def test_ai_generated_likely_result_is_persisted_and_shown_independently_of_verdict(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        llm.responder = _TurnScript(
            claims_response=make_tool_response("record_claims", {"claims": [{"text": "The scheme was announced.", "category": "gov_scheme"}]}),
            synthesis_response=make_tool_response(
                "record_synthesis",
                {"evidence_stances": ["supporting"], "suggested_result": "verified", "claim_confidence": 0.9, "reasoning_text": "Confirmed."},
            ),
            investigation_turns=[make_tool_response("search_evidence", {"query": "scheme"})],
        )
        search = FakeEvidenceSearchProvider(responder=lambda q: [SearchResult(title="Official", url="https://pib.gov.in/x", snippet="Confirmed.")])
        media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4"))
        stt_provider = _stt_provider_returning(STTResult(status=STTStatus.SUCCESS, transcript="The scheme was announced.", detected_language="en"))
        forensics_provider = _forensics_provider_returning(
            VideoForensicsResult(
                status=VideoForensicsStatus.AI_GENERATED_LIKELY,
                ai_generated_probability=0.94,
                provider_name="some_deepfake_detector",
                model_version="v1",
            )
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), stt_provider, _no_text_ocr_provider(), forensics_provider)
        outcome = await pipeline.run(user=user, media_id="video-13", source_wamid="wamid.v13", language="en")

        # Fact-check verdict (verified) and media authenticity (likely AI) are
        # BOTH present and NEITHER influences the other.
        assert outcome.analysis.overall_result == "verified"
        assert "AI-generated/manipulated media likely" in outcome.reply_text

        forensics_result = await db_session.execute(select(MediaForensicsResult))
        rows = forensics_result.scalars().all()
        assert len(rows) == 1
        assert rows[0].tool_name == "video_deepfake"
        assert rows[0].provider_name == "some_deepfake_detector"
        assert float(rows[0].ai_generated_probability) == 0.94
