"""
Proves AudioPipeline/VideoPipeline correctly integrate a REAL provider
class (SarvamSpeechToTextProvider, backed by httpx.MockTransport) through
the exact same STTTool/SpeechToTextProvider Protocol boundary the fake
providers use everywhere else in this test suite — the pipelines never
need to know or care which concrete class they're talking to. This is
what "do not hard-code the provider into the pipeline" actually proves:
swapping FakeSTTProvider for SarvamSpeechToTextProvider requires ZERO
changes to AudioPipeline/VideoPipeline.

Complements tests/integration/{test_audio_pipeline,test_video_pipeline}.py
(which exhaustively cover pipeline BRANCHING logic via fast, deterministic
fakes) — this file exists only to prove the wiring itself is real, not to
re-test branch coverage already covered there.
"""

import httpx

from app.agent.audio_pipeline import AudioPipeline, AudioPipelineDependencies
from app.agent.safety_gate import SafetyCheckResult, SafetyGate, SafetyOutcome
from app.agent.tools.audio_forensics import AudioForensicsTool, NullAudioForensicsProvider
from app.agent.tools.ocr import OCRTool
from app.agent.tools.speech_to_text import STTTool
from app.agent.tools.video_forensics import NullVideoForensicsProvider, VideoForensicsTool
from app.agent.user_service import get_or_create_user
from app.agent.video_pipeline import VideoPipeline, VideoPipelineDependencies
from app.core.config import Settings
from app.integrations.speech_to_text_client import SarvamSpeechToTextProvider

from tests.conftest import (
    TEST_PHONE_ENCRYPTION_KEY,
    TEST_PHONE_PEPPER,
    FakeEvidenceSearchProvider,
    FakeLLMClient,
    FakeMediaClient,
    make_sample_mp4_bytes,
    make_sample_ogg_bytes,
    make_tool_response,
)
import fakeredis.aioredis


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
        MAX_AUDIO_DURATION_SECONDS=600.0,
        MAX_VIDEO_DURATION_SECONDS=300.0,
        MAX_VIDEO_FRAMES=8,
        VIDEO_FRAME_SAMPLING_INTERVAL_SECONDS=1.0,
        MAX_IMAGE_PIXELS=40_000_000,
    )
    base.update(overrides)
    return Settings(**base)


def _passed_safety_provider():
    class _P:
        async def check(self, media_bytes, mime_type):
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    return _P()


async def test_audio_pipeline_uses_the_real_sarvam_provider_end_to_end(db_session, seeded_registry):
    def sarvam_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "req-1",
                "transcript": "The scheme was announced.",
                "language_code": "en-IN",
                "language_probability": 0.93,
            },
        )

    real_stt = SarvamSpeechToTextProvider(
        api_key="real-looking-key", model="saaras:v3", timeout_seconds=5.0,
        transport=httpx.MockTransport(sarvam_handler),
    )

    llm = FakeLLMClient()
    llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))

    deps = AudioPipelineDependencies(
        llm=llm,
        search_provider=FakeEvidenceSearchProvider(),
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        media_client=media_client,
        safety_gate=SafetyGate(_passed_safety_provider()),
        stt_tool=STTTool(real_stt),  # the REAL provider class, not a fake
        audio_forensics_tool=AudioForensicsTool(NullAudioForensicsProvider()),
    )
    pipeline = AudioPipeline(db_session, deps, _settings())
    user = await get_or_create_user(db_session, "15559990001", TEST_PHONE_PEPPER, TEST_PHONE_ENCRYPTION_KEY)

    outcome = await pipeline.run(user=user, media_id="audio-real-1", source_wamid="wamid.real1", language="en")

    # The transcript genuinely came from SarvamSpeechToTextProvider's real
    # HTTP-response-parsing code, not a scripted fake — proven by it
    # appearing in the reply exactly as the mock server returned it.
    assert "The scheme was announced." in outcome.reply_text


async def test_audio_pipeline_handles_real_provider_auth_failure_gracefully(db_session, seeded_registry):
    def sarvam_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    real_stt = SarvamSpeechToTextProvider(
        api_key="wrong-key", model="saaras:v3", timeout_seconds=5.0,
        transport=httpx.MockTransport(sarvam_handler),
    )

    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))

    deps = AudioPipelineDependencies(
        llm=llm,
        search_provider=FakeEvidenceSearchProvider(),
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        media_client=media_client,
        safety_gate=SafetyGate(_passed_safety_provider()),
        stt_tool=STTTool(real_stt),
        audio_forensics_tool=AudioForensicsTool(NullAudioForensicsProvider()),
    )
    pipeline = AudioPipeline(db_session, deps, _settings())
    user = await get_or_create_user(db_session, "15559990002", TEST_PHONE_PEPPER, TEST_PHONE_ENCRYPTION_KEY)

    outcome = await pipeline.run(user=user, media_id="audio-real-2", source_wamid="wamid.real2", language="en")

    # SarvamSpeechToTextProvider raises SpeechToTextProviderUnavailable on
    # 401 -> STTTool catches it -> PROVIDER_UNAVAILABLE -> AudioPipeline's
    # dedicated transcription-failed reply, never a crash, never a
    # fabricated transcript.
    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert "couldn't reliably transcribe" in outcome.reply_text


async def test_video_pipeline_uses_the_real_sarvam_provider_for_its_audio_track(db_session, seeded_registry):
    def sarvam_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"request_id": "req-1", "transcript": "The sky is blue.", "language_code": "en-IN"},
        )

    real_stt = SarvamSpeechToTextProvider(
        api_key="real-looking-key", model="saaras:v3", timeout_seconds=5.0,
        transport=httpx.MockTransport(sarvam_handler),
    )

    llm = FakeLLMClient()
    llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4"))

    from app.agent.tools.ocr import NullOCRProvider

    deps = VideoPipelineDependencies(
        llm=llm,
        search_provider=FakeEvidenceSearchProvider(),
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        media_client=media_client,
        safety_gate=SafetyGate(_passed_safety_provider()),
        stt_tool=STTTool(real_stt),  # the REAL provider class, not a fake
        ocr_tool=OCRTool(NullOCRProvider()),
        video_forensics_tool=VideoForensicsTool(NullVideoForensicsProvider()),
    )
    pipeline = VideoPipeline(db_session, deps, _settings())
    user = await get_or_create_user(db_session, "15559990003", TEST_PHONE_PEPPER, TEST_PHONE_ENCRYPTION_KEY)

    outcome = await pipeline.run(user=user, media_id="video-real-1", source_wamid="wamid.real3", language="en")

    assert "The sky is blue." in outcome.reply_text
