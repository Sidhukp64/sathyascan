"""
Full audio-pipeline scenario tests, exercised directly against AudioPipeline
(mirrors tests/integration/test_image_pipeline.py's structure) so each
scenario can script the fakes precisely. Uses a real in-memory SQLite DB so
persistence (transcripts, media_attachments, media_forensics_results,
claims) is tested against real queries, not mocks.
"""

import fakeredis.aioredis
from sqlalchemy import select

from app.agent.audio_pipeline import AudioPipeline, AudioPipelineDependencies
from app.agent.safety_gate import SafetyCheckResult, SafetyGate, SafetyOutcome
from app.agent.tools.audio_forensics import AudioForensicsResult, AudioForensicsStatus, AudioForensicsTool
from app.agent.tools.speech_to_text import STTResult, STTStatus, STTTool
from app.agent.user_service import get_or_create_user
from app.core.config import Settings
from app.integrations.evidence_search_client import SearchResult
from app.media.downloader import MediaTooLargeError, UnsafeMediaURLError
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.safety_gate_event import SafetyGateEvent
from app.models.transcript import Transcript

from tests.conftest import (
    TEST_PHONE_ENCRYPTION_KEY,
    TEST_PHONE_PEPPER,
    FakeEvidenceSearchProvider,
    FakeLLMClient,
    FakeMediaClient,
    make_sample_ogg_bytes,
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
        MAX_LLM_TOOL_CALLS_PER_ANALYSIS=10,
        DAILY_SPEND_CIRCUIT_BREAKER_USD=50.0,
        GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT=20,
        MIN_CONCURRING_TIER2_SOURCES=2,
        MAX_AUDIO_DURATION_SECONDS=600.0,
        MAX_UPLOAD_SIZE_BYTES_AUDIO=26_214_400,
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


def _pipeline(db_session, settings, llm, search, media_client, safety_provider, stt_provider, forensics_provider) -> AudioPipeline:
    deps = AudioPipelineDependencies(
        llm=llm,
        search_provider=search,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        media_client=media_client,
        safety_gate=SafetyGate(safety_provider),
        stt_tool=STTTool(stt_provider),
        audio_forensics_tool=AudioForensicsTool(forensics_provider),
    )
    return AudioPipeline(db_session, deps, settings)


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


def _unavailable_forensics_provider():
    class _P:
        async def analyze(self, audio_bytes, mime_type):
            return AudioForensicsResult(status=AudioForensicsStatus.PROVIDER_UNAVAILABLE)

    return _P()


def _forensics_provider_returning(result: AudioForensicsResult):
    class _P:
        async def analyze(self, audio_bytes, mime_type):
            return result

    return _P()


async def test_full_pipeline_transcript_to_classification(db_session, seeded_registry):
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
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(
        STTResult(status=STTStatus.SUCCESS, transcript="The scheme was announced.", detected_language="en", confidence=0.9, provider_name="fake_stt")
    )

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-1", source_wamid="wamid.a1", language="en")

    assert outcome.analysis.status == "completed"
    assert outcome.analysis.overall_result == "verified"
    assert outcome.analysis.input_type == "audio"
    assert outcome.analysis.input_text == ""  # transcript lives in `transcripts`, not duplicated here
    assert "SathyaScan Audio Result" in outcome.reply_text
    assert "The scheme was announced." in outcome.reply_text
    assert "AI-generation detection unavailable" in outcome.reply_text

    transcript_result = await db_session.execute(select(Transcript))
    transcripts = transcript_result.scalars().all()
    assert len(transcripts) == 1
    assert transcripts[0].text == "The scheme was announced."
    assert transcripts[0].language == "en"

    media_result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.analysis_id == outcome.analysis.id))
    media_attachment = media_result.scalars().one()
    assert media_attachment.media_type == "audio"
    assert media_attachment.storage_path is None  # no blob storage, same as images


async def test_unsafe_audio_is_stopped_before_any_analysis(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(STTResult(status=STTStatus.SUCCESS, transcript="should never be read"))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _blocked_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-2", source_wamid="wamid.a2", language="en")

    assert outcome.analysis.status == "blocked"
    assert llm.call_log == []

    result = await db_session.execute(select(SafetyGateEvent).where(SafetyGateEvent.analysis_id == outcome.analysis.id))
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].outcome == "blocked"

    transcript_result = await db_session.execute(select(Transcript))
    assert transcript_result.scalars().all() == []  # never even attempted


async def test_transcription_failure_yields_insufficient_evidence_with_dedicated_reply(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(STTResult(status=STTStatus.FAILED))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-3", source_wamid="wamid.a3", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "infra_error"
    assert "couldn't reliably transcribe" in outcome.reply_text
    assert "Insufficient" in outcome.reply_text or "insufficient" in outcome.reply_text.lower()


async def test_stt_provider_unavailable_yields_same_dedicated_reply(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(STTResult(status=STTStatus.PROVIDER_UNAVAILABLE))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-4", source_wamid="wamid.a4", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "provider_unavailable"
    assert "couldn't reliably transcribe" in outcome.reply_text


async def test_no_speech_found_is_a_friendly_reply_not_insufficient_evidence(db_session, seeded_registry):
    """decisions.md §1A distinction: STT running successfully and finding
    nothing is a completed investigation with nothing to check — NOT the
    same as the investigation failing to complete."""
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(STTResult(status=STTStatus.NO_SPEECH_FOUND))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-5", source_wamid="wamid.a5", language="en")

    assert outcome.analysis.overall_result == "no_claims_detected"
    assert outcome.analysis.status == "completed"
    assert "couldn't detect any speech" in outcome.reply_text


async def test_no_claims_in_transcript_yields_no_claims_detected(db_session, seeded_registry):
    llm = FakeLLMClient()
    llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(
        STTResult(status=STTStatus.SUCCESS, transcript="Just saying hello to everyone.", detected_language="en")
    )

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-6", source_wamid="wamid.a6", language="en")

    assert outcome.analysis.overall_result == "no_claims_detected"
    assert "couldn't find a specific factual claim" in outcome.reply_text


async def test_budget_limit_before_stt_yields_insufficient_evidence(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_calls: list[str] = []

    class _CountingSTT:
        async def transcribe(self, audio_bytes, mime_type, language_hint):
            stt_calls.append("called")
            return STTResult(status=STTStatus.SUCCESS, transcript="x")

    user = await _make_user(db_session)
    settings = _settings(MAX_LLM_TOOL_CALLS_PER_ANALYSIS=0)
    pipeline = _pipeline(db_session, settings, llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _CountingSTT(), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-7", source_wamid="wamid.a7", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "tool_call_limit"
    assert stt_calls == []  # STT itself was never reached


async def test_oversized_audio_is_a_friendly_pre_analysis_error(db_session, seeded_registry):
    llm = FakeLLMClient()

    def _raise(mid):
        raise MediaTooLargeError("too big")

    media_client = FakeMediaClient(responder=_raise)

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _stt_provider_returning(STTResult(status=STTStatus.SUCCESS)), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-8", source_wamid="wamid.a8", language="en")

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
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _stt_provider_returning(STTResult(status=STTStatus.SUCCESS)), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-9", source_wamid="wamid.a9", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "unsafe_url"


async def test_duration_too_long_is_rejected_before_safety_gate(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(duration_seconds=5), "audio/ogg"))
    safety_provider_called = []

    class _TrackedSafety:
        async def check(self, media_bytes, mime_type):
            safety_provider_called.append(True)
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    user = await _make_user(db_session)
    settings = _settings(MAX_AUDIO_DURATION_SECONDS=1.0)
    pipeline = _pipeline(db_session, settings, llm, FakeEvidenceSearchProvider(), media_client, _TrackedSafety(), _stt_provider_returning(STTResult(status=STTStatus.SUCCESS)), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-10", source_wamid="wamid.a10", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "duration_too_long"
    assert safety_provider_called == []  # validation happens before the safety gate
    assert "too long" in outcome.reply_text.lower()


async def test_unsupported_format_is_rejected_after_download_before_safety_gate(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (b"not real audio bytes", "audio/ogg"))
    safety_provider_called = []

    class _TrackedSafety:
        async def check(self, media_bytes, mime_type):
            safety_provider_called.append(True)
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _TrackedSafety(), _stt_provider_returning(STTResult(status=STTStatus.SUCCESS)), _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-11", source_wamid="wamid.a11", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "unsupported_format"
    assert safety_provider_called == []


async def test_no_audio_bytes_are_ever_persisted(db_session, seeded_registry):
    """Phase 5 stores nothing durable beyond the transcript text — same
    'no blob storage' precedent Phase 3 set for images."""
    llm = FakeLLMClient()
    llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
    stt_provider = _stt_provider_returning(STTResult(status=STTStatus.NO_SPEECH_FOUND))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
    outcome = await pipeline.run(user=user, media_id="audio-12", source_wamid="wamid.a12", language="en")

    media_result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.analysis_id == outcome.analysis.id))
    attachment = media_result.scalars().one()
    assert attachment.storage_path is None


class TestAudioForensicsResultHandling:
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
        media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
        stt_provider = _stt_provider_returning(STTResult(status=STTStatus.SUCCESS, transcript="The scheme was announced.", detected_language="en"))

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), stt_provider, _unavailable_forensics_provider())
        outcome = await pipeline.run(user=user, media_id="audio-13", source_wamid="wamid.a13", language="en")

        # Veracity verdict is unaffected by forensics being unavailable.
        assert outcome.analysis.overall_result == "verified"
        assert "AI-generation detection unavailable" in outcome.reply_text

        forensics_result = await db_session.execute(select(MediaForensicsResult))
        assert forensics_result.scalars().all() == []  # absence of row = no analysis attempted (Phase 3 precedent)

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
        media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))
        stt_provider = _stt_provider_returning(STTResult(status=STTStatus.SUCCESS, transcript="The scheme was announced.", detected_language="en"))
        forensics_provider = _forensics_provider_returning(
            AudioForensicsResult(
                status=AudioForensicsStatus.AI_GENERATED_LIKELY,
                ai_generated_probability=0.91,
                provider_name="some_voice_detector",
                model_version="v1",
            )
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), stt_provider, forensics_provider)
        outcome = await pipeline.run(user=user, media_id="audio-14", source_wamid="wamid.a14", language="en")

        # Fact-check verdict (verified) and media authenticity (likely AI) are
        # BOTH present and NEITHER influences the other — the user's explicit
        # "never confuse these two" requirement.
        assert outcome.analysis.overall_result == "verified"
        assert "AI-generated/manipulated media likely" in outcome.reply_text

        forensics_result = await db_session.execute(select(MediaForensicsResult))
        rows = forensics_result.scalars().all()
        assert len(rows) == 1
        assert rows[0].tool_name == "audio_voice_detect"
        assert rows[0].provider_name == "some_voice_detector"
        assert float(rows[0].ai_generated_probability) == 0.91
