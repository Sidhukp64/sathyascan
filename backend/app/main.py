"""
SathyaScan backend — Phase 1 (WhatsApp integration foundation) + Phase 2
(text fact-checking pipeline) + Phase 3 (safety gate + image/OCR pipeline) +
Phase 4 (URL Analyzer + URL Safety Module).

Phase 3 adds: the Meta media downloader, the safety gate, and the OCR /
image-analysis tool clients, all built once at startup and stored on
app.state alongside Phase 1/2's clients — see docs/decisions.md and
docs/phased-plan.md. Real credentials for OCR/image-forensics/safety-gate
providers do not exist in this environment; only Null stub providers are
wired (see app/agent/safety_gate.py, app/agent/tools/{ocr,image_analysis}.py
docstrings for the documented, flagged gap against decisions.md §6/§11/§2).

Phase 4 adds: the SSRF-protected URL fetcher, URL Analyzer, and URL Safety
tool. Unlike Phase 3's providers, MOST of Phase 4 is wired for real — the
fetcher, robots.txt check, HTML extraction, and WHOIS-based domain-age
checker all need no credentials at all. Only the optional Safe
Browsing-style threat-intel check is a Null stub (see
app/agent/tools/url_safety.py's docstring).

Phase 5a adds: a SEPARATE MetaMediaClient for audio (its own size limit —
see app/webhook/whatsapp/router.py's get_audio_media_client docstring for
why a second instance is needed), the Speech-to-Text tool, and the audio-
forensics tool.

Phase 5b adds: a THIRD MetaMediaClient for video (its own size limit,
100MB), and the video-forensics tool. VideoPipeline reuses app.state.stt_tool
AND app.state.ocr_tool directly — no second Speech-to-Text or OCR engine,
per the user's explicit instruction not to build a second fact-checking/OCR
system.

Phase "A/B/C-real" adds genuine provider implementations for all three
(Sarvam AI for STT, Resemble AI Detect for audio/video forensics — see
app/integrations/{speech_to_text_client,media_forensics_client}.py for the
provider-selection reasoning) — but selection is OPT-IN and centralized in
app/core/provider_selection.py: Null* stays the default for each unless its
`<X>_PROVIDER` + `<X>_API_KEY` are both set. No provider here has been
credential-tested (none exist in this environment) or benchmarked
(decisions.md §11) — see docs/risks-and-open-questions.md for the full,
honest status of each.

Phase 6 adds the dashboard REST API (backend only, no frontend — confirmed
scope decision): JWT-authenticated auth routes under `/api/v1`, see
app/api/v1/routers/*. WhatsApp-OTP dashboard login reuses the existing
WhatsAppSender but is a genuinely proactive send outside any inbound
conversation window — a documented, confirmed conflict with decisions.md §4,
see app/api/v1/routers/auth.py's module docstring. History/settings/privacy-
purge routes are still in progress — see docs/risks-and-open-questions.md
and backend/README.md for the current, honest state of what's wired here.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy import text as sql_text

from app.agent.explore_rollup import run_explore_rollup
from app.agent.retention import run_retention_purge
from app.agent.scheduled_check_runner import run_due_scheduled_checks
from app.agent.safety_gate import NullSafetyProvider, SafetyGate
from app.agent.tools.audio_forensics import AudioForensicsTool, NullAudioForensicsProvider
from app.agent.tools.image_analysis import ImageAnalysisTool, NullImageForensicsProvider
from app.agent.tools.ocr import NullOCRProvider, OCRTool
from app.agent.tools.speech_to_text import NullSpeechToTextProvider, STTTool
from app.agent.tools.url_analyzer import UrlAnalyzerTool
from app.agent.tools.url_safety import NullThreatIntelProvider, UrlSafetyTool
from app.agent.tools.video_forensics import NullVideoForensicsProvider, VideoForensicsTool
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.http_hardening import RequestSizeLimitMiddleware, SecurityHeadersMiddleware
from app.core.job_status import job_status
from app.core.logging import configure_logging
from app.core.provider_selection import (
    build_audio_forensics_provider,
    build_ocr_provider,
    build_speech_to_text_provider,
    build_video_forensics_provider,
)
from app.api.v1.routers.account import router as account_router
from app.api.v1.routers.analysis_sessions import router as analysis_sessions_router
from app.api.v1.routers.appeals import router as appeals_router
from app.api.v1.routers.auth import router as dashboard_auth_router
from app.api.v1.routers.explore import router as explore_router
from app.api.v1.routers.history import router as dashboard_history_router
from app.api.v1.routers.moderation import router as moderation_router
from app.api.v1.routers.notifications import router as notifications_router
from app.api.v1.routers.overview import router as overview_router
from app.api.v1.routers.scheduled_checks import router as dashboard_scheduled_checks_router
from app.api.v1.routers.settings import router as dashboard_settings_router
from app.core.redis_client import build_redis_client
from app.db.session import build_engine, build_sessionmaker, session_scope
from app.integrations.claude_client import AnthropicLLMClient
from app.integrations.evidence_search_client import TavilyEvidenceSearchProvider
from app.media.downloader import MetaMediaClient
from app.web.domain_age import WhoisDomainAgeChecker
from app.web.fetcher import SecureUrlFetcher
from app.webhook.whatsapp.router import router as whatsapp_webhook_router

logger = logging.getLogger(__name__)


async def _run_periodic_retention_purge(app: FastAPI, settings) -> None:
    """Phase 6 — Privacy Mode retention (app/agent/retention.py,
    decisions.md §8). A lightweight in-process asyncio periodic task, NOT
    Celery — matches this project's established BackgroundTasks-over-Celery
    precedent (every prior phase's heavy work already runs this way); the
    architecture note proposing Celery+Redis for scheduling predates every
    phase actually built and was never implemented, same as
    `global_heavy_queue_concurrency_limit`'s documented unused-placeholder
    status.

    Runs forever until cancelled at shutdown. A single purge failure (e.g. a
    transient DB error) is logged and the loop continues on its next tick —
    it must never silently die and stop purging for the rest of the
    process's lifetime.
    """
    while True:
        await asyncio.sleep(settings.retention_purge_interval_seconds)
        try:
            async with session_scope(app.state.db_sessionmaker) as session:
                result = await run_retention_purge(session, settings)
            job_status.record_tick_success("retention_purge")
            if result.media_attachments_deleted or result.analyses_deleted:
                logger.info(
                    "retention purge complete: media_attachments_deleted=%d analyses_deleted=%d",
                    result.media_attachments_deleted,
                    result.analyses_deleted,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let one bad tick kill the loop
            job_status.record_tick_failure("retention_purge", type(exc).__name__)
            logger.exception("retention purge tick failed — will retry on next interval")


async def _run_periodic_scheduled_checks(app: FastAPI, settings) -> None:
    """Phase 8 — "Check This Tomorrow" (decisions.md §14). Same in-process
    asyncio periodic task pattern as retention purge above — NOT Celery, per
    the user's explicit instruction not to introduce another scheduler.
    Reuses app.state.llm_client/search_provider, the SAME clients every
    other pipeline already uses — no second evidence/LLM integration."""
    while True:
        await asyncio.sleep(settings.scheduled_check_execution_interval_seconds)
        try:
            async with session_scope(app.state.db_sessionmaker) as session:
                executed_count = await run_due_scheduled_checks(
                    session, settings, app.state.llm_client, app.state.search_provider
                )
            job_status.record_tick_success("scheduled_check_runner")
            if executed_count:
                logger.info("scheduled-check batch complete: executed=%d", executed_count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let one bad tick kill the loop
            job_status.record_tick_failure("scheduled_check_runner", type(exc).__name__)
            logger.exception("scheduled-check tick failed — will retry on next interval")


async def _run_periodic_explore_rollup(app: FastAPI, settings) -> None:
    """Phase 8 — Explore (docs/api-design.md's "structural privacy
    enforcement"). Same in-process asyncio periodic task pattern. Populates
    `explore_claim_clusters` from recently-completed, non-privacy-mode
    analyses — see app/agent/explore_rollup.py's docstring for why this is
    a read-model rollup, not a live query over analyses/claims."""
    while True:
        await asyncio.sleep(settings.explore_rollup_interval_seconds)
        try:
            async with session_scope(app.state.db_sessionmaker) as session:
                cluster_count = await run_explore_rollup(session, settings)
            job_status.record_tick_success("explore_rollup")
            if cluster_count:
                logger.info("explore rollup complete: clusters_updated=%d", cluster_count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let one bad tick kill the loop
            job_status.record_tick_failure("explore_rollup", type(exc).__name__)
            logger.exception("explore rollup tick failed — will retry on next interval")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Redis: connection is lazy (see core/redis_client.py) — never blocks
    # startup just because Redis isn't reachable yet.
    app.state.redis = build_redis_client(settings.redis_url)

    # Postgres: engine construction is also lazy (no connection until the
    # first query), so startup never fails just because Postgres isn't up
    # yet either — matches the Redis pattern above.
    app.state.db_engine = build_engine(settings.database_url)
    app.state.db_sessionmaker = build_sessionmaker(app.state.db_engine)

    # Claude / evidence search: constructed once and reused across requests.
    # No API key is required to construct these clients (only to call them),
    # so startup succeeds even with empty keys in local/dev without credentials.
    app.state.llm_client = AnthropicLLMClient(settings.anthropic_api_key, settings.anthropic_model)
    app.state.search_provider = TavilyEvidenceSearchProvider(settings.search_api_key)

    # Phase 3: media downloader (real, SSRF-protected — this part works with
    # real Meta credentials) + safety gate / OCR / image-analysis (Null stubs
    # only — see module docstrings for the flagged, documented gaps).
    app.state.media_client = MetaMediaClient(
        graph_base_url=settings.whatsapp_graph_base_url,
        access_token=settings.whatsapp_access_token,
        timeout_seconds=settings.media_download_timeout_seconds,
        max_size_bytes=settings.max_upload_size_bytes_image,
    )
    app.state.safety_gate = SafetyGate(NullSafetyProvider())
    # OCR is now opt-in-selectable (OCR_PROVIDER=claude_vision) rather than
    # hard-wired to the Null stub. It reuses app.state.llm_client — the same
    # Anthropic client every other stage uses — so enabling it adds no new
    # credential and no second engine. Kept on app.state separately from the
    # Tool wrapper so /health and the admin provider view can report the
    # ACTUAL runtime provider, same pattern as STT/forensics below.
    app.state.ocr_provider = build_ocr_provider(settings, app.state.llm_client)
    app.state.ocr_tool = OCRTool(app.state.ocr_provider)
    app.state.image_analysis_tool = ImageAnalysisTool(NullImageForensicsProvider())

    # Phase 4: URL Analyzer / URL Safety — the fetcher, robots.txt check,
    # HTML extraction, and domain-age (WHOIS) checker are all REAL, no
    # credentials needed. Only threat-intel (Safe Browsing-style) is a Null
    # stub — that's the one piece explicitly marked OPTIONAL and requiring a
    # paid API this environment has no credentials for (phased-plan.md).
    app.state.url_fetcher = SecureUrlFetcher(
        timeout_seconds=settings.url_fetch_timeout_seconds,
        max_size_bytes=settings.max_url_fetch_size_bytes,
        max_redirects=settings.max_url_redirects,
        user_agent=settings.url_fetch_user_agent,
    )
    app.state.url_analyzer_tool = UrlAnalyzerTool()
    app.state.url_safety_tool = UrlSafetyTool(
        WhoisDomainAgeChecker(timeout_seconds=settings.whois_lookup_timeout_seconds),
        NullThreatIntelProvider(),
    )

    # Phase 5a: audio. A SEPARATE MetaMediaClient from Phase 3's image one —
    # same SSRF-protected logic, but sized for audio (25MB vs image's 10MB).
    # STT and audio-forensics are both Null stubs (see module docstring).
    app.state.audio_media_client = MetaMediaClient(
        graph_base_url=settings.whatsapp_graph_base_url,
        access_token=settings.whatsapp_access_token,
        timeout_seconds=settings.media_download_timeout_seconds,
        max_size_bytes=settings.max_upload_size_bytes_audio,
    )
    # Kept on app.state separately from the Tool wrapper (not reached via a
    # private attribute) so /health can honestly report the ACTUAL runtime
    # provider selected, not just what config claims — see health() below.
    app.state.stt_provider = build_speech_to_text_provider(settings)
    app.state.stt_tool = STTTool(app.state.stt_provider)
    app.state.audio_forensics_provider = build_audio_forensics_provider(settings)
    app.state.audio_forensics_tool = AudioForensicsTool(app.state.audio_forensics_provider)

    # Phase 5b: video. A THIRD MetaMediaClient — sized for video (100MB).
    # video-forensics is a Null stub (see module docstring). STT and OCR are
    # REUSED from Phase 5a/Phase 3 above — no second engine of either kind.
    app.state.video_media_client = MetaMediaClient(
        graph_base_url=settings.whatsapp_graph_base_url,
        access_token=settings.whatsapp_access_token,
        timeout_seconds=settings.media_download_timeout_seconds,
        max_size_bytes=settings.max_upload_size_bytes_video,
    )
    app.state.video_forensics_provider = build_video_forensics_provider(settings)
    app.state.video_forensics_tool = VideoForensicsTool(app.state.video_forensics_provider)

    # Phase 6: Privacy Mode retention purge — see _run_periodic_retention_purge's
    # docstring. Started here so it runs for the life of the process; tests
    # never await app startup through create_app()+lifespan without also
    # tearing it down (tests/conftest.py's `client`/`auth_env` fixtures both
    # exit the `async with app.router.lifespan_context(app)` block, which
    # triggers the cancellation below), so this never leaks a background
    # task across tests.
    retention_task = asyncio.create_task(_run_periodic_retention_purge(app, settings))
    # Phase 8: same pattern, two more periodic tasks — "Check This Tomorrow"
    # execution and the Explore rollup. See their own docstrings.
    scheduled_checks_task = asyncio.create_task(_run_periodic_scheduled_checks(app, settings))
    explore_rollup_task = asyncio.create_task(_run_periodic_explore_rollup(app, settings))
    background_tasks = [retention_task, scheduled_checks_task, explore_rollup_task]

    logger.info("SathyaScan backend starting (env=%s)", settings.env)
    yield
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await app.state.redis.aclose()
    await app.state.db_engine.dispose()
    logger.info("SathyaScan backend shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="SathyaScan Backend",
        description=(
            "SathyaScan backend — through Phase 9 (Production Completion & Trust: admin/appeals/"
            "moderation, provider/job monitoring, account suspension). Backend only, no frontend. See "
            "backend/README.md for the verified/blocked status of every component."
        ),
        version="0.9.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    # Phase 7 — app/core/http_hardening.py. Verified (not just implemented):
    # a 413 from RequestSizeLimitMiddleware still carries the security
    # headers SecurityHeadersMiddleware adds, and a normal response carries
    # both — see tests/integration/test_http_hardening.py.
    app.add_middleware(RequestSizeLimitMiddleware, max_body_bytes=get_settings().dashboard_max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(whatsapp_webhook_router)
    app.include_router(dashboard_auth_router)
    app.include_router(dashboard_history_router)
    app.include_router(dashboard_settings_router)
    app.include_router(dashboard_scheduled_checks_router)
    app.include_router(explore_router)
    app.include_router(notifications_router)
    app.include_router(overview_router)
    app.include_router(analysis_sessions_router)
    app.include_router(account_router)
    app.include_router(appeals_router)
    app.include_router(moderation_router)
    dashboard_index_path = Path(__file__).resolve().parent.parent.parent / "dashboard" / "index.html"

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    async def serve_dashboard():
        if dashboard_index_path.is_file():
            return FileResponse(dashboard_index_path)
        return {"message": "SathyaScan Multimodal Trust Platform API"}

    @app.get("/health")
    async def health():
        db_ok = True
        try:
            async with app.state.db_engine.connect() as conn:
                await conn.execute(sql_text("SELECT 1"))
        except Exception:  # noqa: BLE001 - health check must never raise
            db_ok = False

        redis_ok = True
        try:
            await app.state.redis.ping()
        except Exception:  # noqa: BLE001
            redis_ok = False

        settings = get_settings()
        return {
            "status": "ok" if (db_ok and redis_ok) else "degraded",
            "phase": 9,
            "db": db_ok,
            "redis": redis_ok,
            # Loudly visible operational flag — decisions.md §6: a deployment
            # with no real safety-gate provider configured must not be public.
            "safety_gate_provider_configured": bool(settings.safety_gate_provider),
            # Phase 6 — an empty JWT_SECRET means every dashboard auth call
            # fails closed (create_access_token refuses to sign, see
            # app/core/jwt_auth.py), which is safe but silent; surfaced here
            # for the same reason as the safety-gate flag above.
            "jwt_configured": bool(settings.jwt_secret),
            # Phase 7 — same "silent fail-closed, so surface it" reasoning
            # extended to the other two secrets decisions.md §7 names
            # explicitly: an empty PHONE_HASH_PEPPER makes hash_phone_number/
            # hash_otp_code raise (every webhook message AND every OTP
            # request fails), an empty PHONE_ENCRYPTION_KEY makes
            # get_or_create_user's encrypt_phone_number call raise (every
            # new user fails to be created). Neither was previously visible
            # from any single endpoint.
            "phone_hash_pepper_configured": bool(settings.phone_hash_pepper),
            "phone_encryption_key_configured": bool(settings.phone_encryption_key),
            # Phase 5 final-validation addition: reports the ACTUAL runtime
            # provider (introspected from app.state, not re-derived from
            # config) so "is this deployment using a real STT/forensics
            # provider or the Null stub" is answerable from one endpoint
            # without reading logs — same transparency precedent as the
            # safety-gate flag above. "real" here means the concrete class
            # is wired, NOT that it has been credential-tested or
            # benchmarked (decisions.md §11/§2 still apply).
            "ocr_provider": "null" if isinstance(app.state.ocr_provider, NullOCRProvider) else "real",
            "speech_to_text_provider": "null" if isinstance(app.state.stt_provider, NullSpeechToTextProvider) else "real",
            "audio_forensics_provider": (
                "null" if isinstance(app.state.audio_forensics_provider, NullAudioForensicsProvider) else "real"
            ),
            "video_forensics_provider": (
                "null" if isinstance(app.state.video_forensics_provider, NullVideoForensicsProvider) else "real"
            ),
        }

    return app


app = create_app()
