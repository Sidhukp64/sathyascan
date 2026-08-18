import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import av
import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.safety_gate import SafetyCheckResult, SafetyGate, SafetyOutcome
from app.agent.tools.audio_forensics import AudioForensicsResult, AudioForensicsStatus, AudioForensicsTool
from app.agent.tools.image_analysis import ImageAnalysisResult, ImageAnalysisStatus, ImageAnalysisTool
from app.agent.tools.ocr import OCRResult, OCRStatus, OCRTool
from app.agent.tools.speech_to_text import STTResult, STTStatus, STTTool
from app.agent.tools.url_analyzer import UrlAnalyzerTool
from app.agent.tools.url_safety import NullThreatIntelProvider, UrlSafetyTool
from app.agent.tools.video_forensics import VideoForensicsResult, VideoForensicsStatus, VideoForensicsTool
from app.core.config import Settings, get_settings
from app.db.session import build_sessionmaker
from app.integrations.claude_client import (
    LLMContentBlock,
    LLMProviderError,
    LLMProviderTimeout,
    LLMResponse,
    LLMTextBlock,
    LLMToolUseBlock,
)
from app.integrations.evidence_search_client import (
    EvidenceSearchProviderError,
    EvidenceSearchProviderTimeout,
    SearchResult,
)
from app.main import create_app
from app.models import Base
from app.models.source_credibility import (
    TIER_1_GOV_OFFICIAL,
    TIER_2_REPUTABLE_NEWS,
    SourceCredibilityRegistry,
)
from app.web.domain_age import DomainAgeResult, DomainAgeStatus
from app.web.fetcher import FetchResult
from app.webhook.whatsapp.router import (
    get_audio_forensics_tool,
    get_audio_media_client,
    get_db_sessionmaker,
    get_image_analysis_tool,
    get_llm_client,
    get_media_client,
    get_ocr_tool,
    get_safety_gate,
    get_search_provider,
    get_sender,
    get_stt_tool,
    get_url_analyzer_tool,
    get_url_fetcher,
    get_url_safety_tool,
    get_video_forensics_tool,
    get_video_media_client,
)

TEST_APP_SECRET = "test-app-secret"
TEST_VERIFY_TOKEN = "test-verify-token"
TEST_PHONE_PEPPER = "test-phone-pepper"
TEST_PHONE_ENCRYPTION_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 bytes, base64
# Phase 6 — dashboard JWT auth (app/core/jwt_auth.py). Added to the shared
# test_settings fixture below rather than a separate one: nothing before
# Phase 6 reads jwt_secret, so this is purely additive to existing tests.
TEST_JWT_SECRET = "test-jwt-secret-for-integration-tests-do-not-use-in-prod"


def make_sample_jpeg_bytes(width: int = 20, height: int = 20) -> bytes:
    """A real, tiny, validly-decodable JPEG — generated with Pillow itself,
    not a hand-rolled fixture, so validate_image() genuinely exercises real
    decode logic in tests rather than trusting a canned byte string."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 0, 0)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_sample_ogg_bytes(duration_seconds: float = 2.0, sample_rate: int = 48000) -> bytes:
    """A real, tiny, validly-decodable OGG/Opus audio clip — encoded with
    PyAV itself (silence), not a hand-rolled fixture, so validate_audio()
    and the STT/forensics pipeline genuinely exercise real container-parsing
    logic in tests rather than trusting a canned byte string. Mirrors
    make_sample_jpeg_bytes()'s exact rationale."""
    buffer = io.BytesIO()
    output = av.open(buffer, mode="w", format="ogg")
    stream = output.add_stream("libopus", rate=sample_rate)
    frame_size = 960
    n_frames = max(1, int(duration_seconds * sample_rate / frame_size))
    for _ in range(n_frames):
        frame = av.AudioFrame(format="s16", layout="mono", samples=frame_size)
        frame.sample_rate = sample_rate
        for plane in frame.planes:
            plane.update(bytes(frame_size * 2))
        for packet in stream.encode(frame):
            output.mux(packet)
    for packet in stream.encode(None):
        output.mux(packet)
    output.close()
    return buffer.getvalue()


def make_sample_mp4_bytes(
    duration_seconds: float = 3.0,
    width: int = 64,
    height: int = 64,
    fps: int = 10,
    with_audio: bool = True,
) -> bytes:
    """A real, tiny, validly-decodable MP4 clip (video + optional audio
    track) — encoded with PyAV itself (blank frames/silence), not a
    hand-rolled fixture, so validate_video()/frame_extractor.py/
    audio_extractor.py genuinely exercise real container-parsing logic in
    tests rather than trusting a canned byte string. Mirrors
    make_sample_jpeg_bytes()/make_sample_ogg_bytes()'s exact rationale."""
    buffer = io.BytesIO()
    output = av.open(buffer, mode="w", format="mp4")
    vstream = output.add_stream("libx264", rate=fps)
    vstream.width = width
    vstream.height = height
    vstream.pix_fmt = "yuv420p"
    astream = output.add_stream("aac", rate=44100) if with_audio else None

    n_video_frames = max(1, int(duration_seconds * fps))
    for i in range(n_video_frames):
        frame = av.VideoFrame(width, height, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = i
        for packet in vstream.encode(frame):
            output.mux(packet)

    if astream is not None:
        n_audio_frames = max(1, int(duration_seconds * 44100 / 1024))
        for _ in range(n_audio_frames):
            aframe = av.AudioFrame(format="fltp", layout="mono", samples=1024)
            aframe.sample_rate = 44100
            for plane in aframe.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in astream.encode(aframe):
                output.mux(packet)

    for packet in vstream.encode(None):
        output.mux(packet)
    if astream is not None:
        for packet in astream.encode(None):
            output.mux(packet)
    output.close()
    return buffer.getvalue()


class FakeWhatsAppSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_text_message(self, to_phone: str, phone_hash: str, body: str) -> None:
        self.sent.append((to_phone, phone_hash, body))


@dataclass
class LLMCallContext:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    tool_choice: dict[str, Any] | None

    @property
    def forced_tool_name(self) -> str | None:
        return self.tool_choice["name"] if self.tool_choice else None


def make_tool_response(tool_name: str, tool_input: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=[LLMToolUseBlock(tool_use_id=f"toolu_{tool_name}", tool_name=tool_name, tool_input=tool_input)],
        stop_reason="tool_use",
    )


def make_text_response(text: str = "Done investigating.") -> LLMResponse:
    return LLMResponse(content=[LLMTextBlock(text=text)], stop_reason="end_turn")


@dataclass
class FakeLLMClient:
    """Scripted fake — tests set `.responder` to a function of
    (LLMCallContext) -> LLMResponse dispatching on `ctx.forced_tool_name` /
    `ctx.tools` to control each pipeline stage independently. Defaults to
    always extracting zero claims, so tests that don't care about the AI
    pipeline (e.g. media-message tests) get a harmless no-op if invoked."""

    responder: Callable[[LLMCallContext], LLMResponse] = field(
        default_factory=lambda: (lambda ctx: make_tool_response("record_claims", {"claims": []}))
    )
    call_log: list[LLMCallContext] = field(default_factory=list)

    async def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        ctx = LLMCallContext(system=system, messages=messages, tools=tools, tool_choice=tool_choice)
        self.call_log.append(ctx)
        result = self.responder(ctx)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeEvidenceSearchProvider:
    """Defaults to returning no results. Tests set `.responder` to a
    function of (query) -> list[SearchResult], or raise an
    EvidenceSearchProviderError/-Timeout to simulate provider failure."""

    responder: Callable[[str], list[SearchResult]] = field(default_factory=lambda: (lambda query: []))
    call_count: int = 0

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.call_count += 1
        result = self.responder(query)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeMediaClient:
    """Defaults to returning a valid sample JPEG. Tests set `.responder` to
    a function of (media_id) -> (bytes, mime_type), or raise one of
    app.media.downloader's exceptions to simulate a download failure."""

    responder: Callable[[str], tuple[bytes, str | None]] = field(
        default_factory=lambda: (lambda media_id: (make_sample_jpeg_bytes(), "image/jpeg"))
    )
    call_count: int = 0

    async def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        self.call_count += 1
        result = self.responder(media_id)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeSafetyProvider:
    """Defaults to always PASSED. Tests set `.responder` to a function of
    (media_bytes, mime_type) -> SafetyCheckResult, or raise to simulate a
    provider failure (exercised through the real SafetyGate wrapper, which
    is what actually enforces fail-closed — see app/agent/safety_gate.py)."""

    responder: Callable[[bytes, str], SafetyCheckResult] = field(
        default_factory=lambda: (
            lambda b, m: SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")
        )
    )

    async def check(self, media_bytes: bytes, mime_type: str) -> SafetyCheckResult:
        result = self.responder(media_bytes, mime_type)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeOCRProvider:
    """Defaults to PROVIDER_UNAVAILABLE (matching the real NullOCRProvider's
    honest default — see app/agent/tools/ocr.py). Tests set `.responder` to
    return a specific OCRResult or raise, exercised through the real OCRTool
    wrapper."""

    responder: Callable[[bytes, str, str | None], OCRResult] = field(
        default_factory=lambda: (
            lambda b, m, lang: OCRResult(status=OCRStatus.PROVIDER_UNAVAILABLE, provider_name="fake")
        )
    )

    async def extract_text(self, image_bytes: bytes, mime_type: str, language_hint: str | None) -> OCRResult:
        result = self.responder(image_bytes, mime_type, language_hint)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeImageAnalysisProvider:
    """Defaults to UNAVAILABLE (matching the real NullImageForensicsProvider
    — see app/agent/tools/image_analysis.py)."""

    responder: Callable[[bytes, str], ImageAnalysisResult] = field(
        default_factory=lambda: (lambda b, m: ImageAnalysisResult(status=ImageAnalysisStatus.UNAVAILABLE))
    )

    async def analyze(self, image_bytes: bytes, mime_type: str) -> ImageAnalysisResult:
        result = self.responder(image_bytes, mime_type)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeSTTProvider:
    """Defaults to PROVIDER_UNAVAILABLE (matching the real
    NullSpeechToTextProvider's honest default — see
    app/agent/tools/speech_to_text.py). Tests set `.responder` to return a
    specific STTResult (e.g. a scripted Malayalam/Tamil/Hindi/English
    transcript) or raise, exercised through the real STTTool wrapper."""

    responder: Callable[[bytes, str, str | None], STTResult] = field(
        default_factory=lambda: (
            lambda b, m, lang: STTResult(status=STTStatus.PROVIDER_UNAVAILABLE, provider_name="fake")
        )
    )

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str | None) -> STTResult:
        result = self.responder(audio_bytes, mime_type, language_hint)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeAudioForensicsProvider:
    """Defaults to PROVIDER_UNAVAILABLE (matching the real
    NullAudioForensicsProvider — see app/agent/tools/audio_forensics.py)."""

    responder: Callable[[bytes, str], AudioForensicsResult] = field(
        default_factory=lambda: (lambda b, m: AudioForensicsResult(status=AudioForensicsStatus.PROVIDER_UNAVAILABLE))
    )

    async def analyze(self, audio_bytes: bytes, mime_type: str) -> AudioForensicsResult:
        result = self.responder(audio_bytes, mime_type)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeVideoForensicsProvider:
    """Defaults to PROVIDER_UNAVAILABLE (matching the real
    NullVideoForensicsProvider — see app/agent/tools/video_forensics.py)."""

    responder: Callable[[bytes, str], VideoForensicsResult] = field(
        default_factory=lambda: (lambda b, m: VideoForensicsResult(status=VideoForensicsStatus.PROVIDER_UNAVAILABLE))
    )

    async def analyze(self, video_bytes: bytes, mime_type: str) -> VideoForensicsResult:
        result = self.responder(video_bytes, mime_type)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeUrlFetcher:
    """Defaults to a valid, minimal HTML page with one checkable sentence.
    Tests set `.responder` to a function of (url) -> FetchResult, or raise
    one of app.web.fetcher's exceptions (UnsafeUrlError, UrlFetchTimeout,
    etc.) to simulate a fetch failure — exercised through the real
    UrlPipeline, which is what actually maps these to reply text/error
    codes."""

    responder: Callable[[str], FetchResult] = field(
        default_factory=lambda: (
            lambda url: FetchResult(
                final_url=url,
                status_code=200,
                content_bytes=(
                    b"<html><head><title>Sample Article</title></head>"
                    b"<body><p>The scheme was announced.</p></body></html>"
                ),
                content_type="text/html; charset=utf-8",
            )
        )
    )
    call_count: int = 0

    async def fetch(self, url: str) -> FetchResult:
        self.call_count += 1
        result = self.responder(url)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeDomainAgeChecker:
    """Defaults to UNAVAILABLE (a real WHOIS lookup never runs in tests —
    see app/web/domain_age.py's live smoke test, kept separate and clearly
    marked, for the one place a real lookup is actually exercised)."""

    responder: Callable[[str], DomainAgeResult] = field(
        default_factory=lambda: (lambda domain: DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE))
    )

    async def check(self, domain: str) -> DomainAgeResult:
        result = self.responder(domain)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        ENV="test",
        LOG_LEVEL="warning",
        WHATSAPP_APP_SECRET=TEST_APP_SECRET,
        WHATSAPP_WEBHOOK_VERIFY_TOKEN=TEST_VERIFY_TOKEN,
        WHATSAPP_ACCESS_TOKEN="test-access-token",
        WHATSAPP_PHONE_NUMBER_ID="1234567890",
        PHONE_HASH_PEPPER=TEST_PHONE_PEPPER,
        PHONE_ENCRYPTION_KEY=TEST_PHONE_ENCRYPTION_KEY,
        ANTHROPIC_API_KEY="test-anthropic-key",
        SEARCH_API_KEY="test-search-key",
        RATE_LIMIT_PER_USER_PER_MINUTE=3,
        IDEMPOTENCY_TTL_SECONDS=60,
        MAX_EVIDENCE_SEARCHES_PER_ANALYSIS=5,
        MAX_LLM_TOOL_CALLS_PER_ANALYSIS=10,
        DAILY_SPEND_CIRCUIT_BREAKER_USD=50.0,
        GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT=20,
        MIN_CONCURRING_TIER2_SOURCES=2,
        DUPLICATE_CONTENT_REUSE_ENABLED=True,
        DUPLICATE_CONTENT_REUSE_WINDOW_HOURS=72,
        JWT_SECRET=TEST_JWT_SECRET,
        JWT_ACCESS_TOKEN_TTL_SECONDS=3600,
        OTP_LENGTH=6,
        OTP_TTL_SECONDS=300,
        OTP_MAX_ATTEMPTS=5,
        OTP_REQUEST_COOLDOWN_SECONDS=60,
        DASHBOARD_RATE_LIMIT_PER_USER_PER_MINUTE=60,
        OTP_START_RATE_LIMIT_PER_IP_PER_MINUTE=10,
    )


@pytest.fixture
def fake_sender() -> FakeWhatsAppSender:
    return FakeWhatsAppSender()


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def fake_search() -> FakeEvidenceSearchProvider:
    return FakeEvidenceSearchProvider()


@pytest.fixture
def fake_media_client() -> FakeMediaClient:
    return FakeMediaClient()


@pytest.fixture
def fake_safety_provider() -> FakeSafetyProvider:
    return FakeSafetyProvider()


@pytest.fixture
def fake_ocr_provider() -> FakeOCRProvider:
    return FakeOCRProvider()


@pytest.fixture
def fake_image_analysis_provider() -> FakeImageAnalysisProvider:
    return FakeImageAnalysisProvider()


@pytest.fixture
def fake_url_fetcher() -> FakeUrlFetcher:
    return FakeUrlFetcher()


@pytest.fixture
def fake_domain_age_checker() -> FakeDomainAgeChecker:
    return FakeDomainAgeChecker()


@pytest.fixture
def fake_audio_media_client() -> FakeMediaClient:
    """A SEPARATE FakeMediaClient instance from fake_media_client's, mirroring
    main.py's real audio_media_client — defaults to a valid sample OGG clip."""
    return FakeMediaClient(responder=lambda mid: (make_sample_ogg_bytes(), "audio/ogg"))


@pytest.fixture
def fake_stt_provider() -> FakeSTTProvider:
    return FakeSTTProvider()


@pytest.fixture
def fake_audio_forensics_provider() -> FakeAudioForensicsProvider:
    return FakeAudioForensicsProvider()


@pytest.fixture
def fake_video_media_client() -> FakeMediaClient:
    """A THIRD SEPARATE FakeMediaClient instance, mirroring main.py's real
    video_media_client — defaults to a valid sample MP4 clip (video + audio)."""
    return FakeMediaClient(responder=lambda mid: (make_sample_mp4_bytes(), "video/mp4"))


@pytest.fixture
def fake_video_forensics_provider() -> FakeVideoForensicsProvider:
    return FakeVideoForensicsProvider()


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(db_engine) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(db_engine)


@pytest_asyncio.fixture
async def db_session(db_sessionmaker: async_sessionmaker[AsyncSession]):
    async with db_sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_registry(db_session: AsyncSession) -> AsyncSession:
    """A small, realistic seed set for tests — not the full production seed
    list (which is content-curation work, see docs/risks-and-open-questions.md)."""
    db_session.add_all(
        [
            SourceCredibilityRegistry(domain="pib.gov.in", credibility_tier=TIER_1_GOV_OFFICIAL, region="IN"),
            SourceCredibilityRegistry(domain="factchecker.example", credibility_tier=TIER_1_GOV_OFFICIAL, region="IN"),
            SourceCredibilityRegistry(domain="reputablenews.example", credibility_tier=TIER_2_REPUTABLE_NEWS),
            SourceCredibilityRegistry(domain="othernews.example", credibility_tier=TIER_2_REPUTABLE_NEWS),
        ]
    )
    await db_session.commit()
    return db_session


@pytest_asyncio.fixture
async def client(
    test_settings,
    fake_sender,
    fake_llm,
    fake_search,
    fake_media_client,
    fake_safety_provider,
    fake_ocr_provider,
    fake_image_analysis_provider,
    fake_url_fetcher,
    fake_domain_age_checker,
    fake_audio_media_client,
    fake_stt_provider,
    fake_audio_forensics_provider,
    fake_video_media_client,
    fake_video_forensics_provider,
):
    """An httpx.AsyncClient over the app, not the sync TestClient — the
    Phase 2 async SQLite engine's connections are event-loop-bound
    (aiosqlite), so schema creation, app lifespan, and every request must
    run in the SAME asyncio event loop. httpx.ASGITransport still runs
    BackgroundTasks to completion before a call returns (same guarantee
    Phase 1's sync TestClient relied on), so nothing about that pattern
    changes for callers — just `await` the request now."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_sender] = lambda: fake_sender
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    app.dependency_overrides[get_search_provider] = lambda: fake_search
    app.dependency_overrides[get_media_client] = lambda: fake_media_client
    app.dependency_overrides[get_safety_gate] = lambda: SafetyGate(fake_safety_provider)
    app.dependency_overrides[get_ocr_tool] = lambda: OCRTool(fake_ocr_provider)
    app.dependency_overrides[get_image_analysis_tool] = lambda: ImageAnalysisTool(fake_image_analysis_provider)
    app.dependency_overrides[get_url_fetcher] = lambda: fake_url_fetcher
    app.dependency_overrides[get_url_analyzer_tool] = lambda: UrlAnalyzerTool()
    app.dependency_overrides[get_url_safety_tool] = lambda: UrlSafetyTool(fake_domain_age_checker, NullThreatIntelProvider())
    app.dependency_overrides[get_audio_media_client] = lambda: fake_audio_media_client
    app.dependency_overrides[get_stt_tool] = lambda: STTTool(fake_stt_provider)
    app.dependency_overrides[get_audio_forensics_tool] = lambda: AudioForensicsTool(fake_audio_forensics_provider)
    app.dependency_overrides[get_video_media_client] = lambda: fake_video_media_client
    app.dependency_overrides[get_video_forensics_tool] = lambda: VideoForensicsTool(fake_video_forensics_provider)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = build_sessionmaker(engine)
    app.dependency_overrides[get_db_sessionmaker] = lambda: sessionmaker

    async with app.router.lifespan_context(app):
        # Same pattern as Phase 1: real (lazy) Redis client from lifespan is
        # replaced with an in-memory fake so no real Redis server is needed.
        app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac

    await engine.dispose()


# ---------------------------------------------------------------------------
# Phase 6 — dashboard API (auth/history/settings/privacy) shared test infra.
# A separate, lighter-weight fixture from `client` above: the dashboard
# surface doesn't touch the image/audio/video/URL provider tools at all, so
# it only overrides get_settings/get_sender/get_db_sessionmaker + fakeredis
# — same override PATTERN as `client`, not a duplicate of its full provider
# wiring (which would be unused dead weight for these tests).
# ---------------------------------------------------------------------------

_OTP_CODE_RE = re.compile(r"\b(\d{6})\b")


def extract_otp_code(message_body: str) -> str:
    match = _OTP_CODE_RE.search(message_body)
    assert match, f"no 6-digit OTP code found in outbound message body: {message_body!r}"
    return match.group(1)


@pytest_asyncio.fixture
async def auth_env(test_settings, fake_sender):
    """Boots a real app instance for Phase 6 dashboard-API tests. Exposes
    `.sessionmaker`/`.redis` directly (not just `.client`) so tests can seed
    or manipulate rows for states unreachable through the HTTP surface alone
    (an already-expired OTP row, a seeded Analysis for another user, etc.)."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_sender] = lambda: fake_sender

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)
    app.dependency_overrides[get_db_sessionmaker] = lambda: sessionmaker

    async with app.router.lifespan_context(app):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        app.state.redis = redis

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield SimpleNamespace(
                client=ac, sessionmaker=sessionmaker, redis=redis, sender=fake_sender, settings=test_settings
            )

    await engine.dispose()


async def login_and_get_token(auth_env, phone_number: str) -> str:
    """Full OTP round trip via the real HTTP surface — returns a valid
    access token. Shared by every Phase 6 test module that needs an
    authenticated caller (history/settings/privacy/audit tests)."""
    start_resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": phone_number})
    assert start_resp.status_code == 200
    code = extract_otp_code(auth_env.sender.sent[-1][2])
    verify_resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": phone_number, "otp_code": code}
    )
    assert verify_resp.status_code == 200
    return verify_resp.json()["access_token"]


async def create_admin_and_get_token(auth_env, email: str, password: str = "test-password-123", role: str = "admin") -> str:
    """Phase 9 — seeds an admin_users row directly (mirrors what
    scripts/create_admin.py does, minus the CLI/getpass layer — no public
    signup endpoint exists, see app/api/v1/routers/admin_auth.py's
    docstring) then logs in via the real HTTP surface. Shared by every
    Phase 9 admin/appeals/moderation test module."""
    from app.core.admin_security import hash_password, normalize_admin_email
    from app.models.admin_user import AdminUser

    async with auth_env.sessionmaker() as session:
        admin = AdminUser(
            email=normalize_admin_email(email), password_hash=hash_password(password), role=role, is_active=True
        )
        session.add(admin)
        await session.commit()

    login_resp = await auth_env.client.post("/api/v1/admin/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 200, login_resp.text
    return login_resp.json()["access_token"]
