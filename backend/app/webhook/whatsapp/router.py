"""
WhatsApp webhook routes: GET verification handshake + POST message receiver.

Flow (see docs/whatsapp-integration.md, docs/architecture.md §1):
  1. Signature verification on the raw body (signature.py) — 403 on failure,
     no processing.
  2. Parse + normalize messages (parser.py), dropping delivery "statuses".
  3. Per message: idempotency check (Redis) -> rate-limit check (Redis) ->
     schedule the reply as a background task -> return 200 immediately.

Phase 2: text messages route to the real fact-checking pipeline
(app/agent/orchestrator.py) instead of Phase 1's fixed echo.

Phase 3: image messages now route to the real safety-gate + OCR pipeline
(app/agent/image_pipeline.py) instead of Phase 1's fixed "not analyzed yet"
reply. Audio/video/document/sticker/unsupported are unchanged — still Phase
1's fixed reply, out of scope (audio is V2, video is V3 per phased-plan.md).

Phase 4: WhatsApp has no distinct "url" message type — a forwarded link
still arrives as a `text` message (see app/agent/url_detection.py's module
docstring). Text messages whose ENTIRE body is a single bare URL now route
to the URL Analyzer + URL Safety pipeline (app/agent/url_pipeline.py)
instead of the plain claim-extraction path; text containing a URL alongside
other commentary is unchanged, still routed to TextPipeline.

Phase 5a: audio messages now route to the real transcription + fact-check
pipeline (app/agent/audio_pipeline.py) instead of Phase 1's fixed "not
analyzed yet" reply.

Phase 5b: video messages now route to the real transcription + OCR +
multimodal fact-check pipeline (app/agent/video_pipeline.py) instead of
Phase 1's fixed "not analyzed yet" reply. Document/sticker/unsupported
remain unchanged, still Phase 1's fixed reply.
"""

import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.audio_pipeline import AudioPipeline, AudioPipelineDependencies
from app.agent.image_pipeline import ImagePipeline, ImagePipelineDependencies
from app.agent.orchestrator import PipelineDependencies, TextPipeline
from app.agent.safety_gate import SafetyGate
from app.agent.tools.audio_forensics import AudioForensicsTool
from app.agent.tools.image_analysis import ImageAnalysisTool
from app.agent.tools.ocr import OCRTool
from app.agent.tools.response_generation import ResponseGenerationTool
from app.agent.tools.speech_to_text import STTTool
from app.agent.tools.url_analyzer import UrlAnalyzerTool
from app.agent.tools.url_safety import UrlSafetyTool
from app.agent.tools.video_forensics import VideoForensicsTool
from app.agent.url_detection import extract_bare_url
from app.agent.url_pipeline import UrlPipeline, UrlPipelineDependencies
from app.agent.user_service import get_or_create_user
from app.agent.video_pipeline import VideoPipeline, VideoPipelineDependencies
from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.core.pipeline_timeout import PipelineTimeoutError, run_with_timeout
from app.core.rate_limit import RateLimiter
from app.db.session import session_scope
from app.integrations.claude_client import LLMClient
from app.integrations.evidence_search_client import EvidenceSearchProvider
from app.media.downloader import MetaMediaClient
from app.web.fetcher import SecureUrlFetcher
from app.webhook.whatsapp.idempotency import IdempotencyGuard
from app.webhook.whatsapp.parser import parse_normalized_messages
from app.webhook.whatsapp.responses import build_rate_limit_reply, build_reply_text
from app.webhook.whatsapp.schemas import NormalizedMessage, WebhookEnvelope
from app.webhook.whatsapp.sender import WhatsAppSendError, WhatsAppSender
from app.webhook.whatsapp.signature import verify_signature_and_get_body

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp-webhook"])


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def get_db_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.db_sessionmaker


def get_llm_client(request: Request) -> LLMClient:
    return request.app.state.llm_client


def get_search_provider(request: Request) -> EvidenceSearchProvider:
    return request.app.state.search_provider


def get_media_client(request: Request) -> MetaMediaClient:
    return request.app.state.media_client


def get_audio_media_client(request: Request) -> MetaMediaClient:
    """A SEPARATE MetaMediaClient instance from get_media_client's — same
    class, same SSRF-protected logic, but configured with
    max_upload_size_bytes_audio instead of the image limit (25MB vs 10MB;
    reusing the image-sized client would silently truncate legitimate
    voice notes). See main.py's lifespan."""
    return request.app.state.audio_media_client


def get_safety_gate(request: Request) -> SafetyGate:
    return request.app.state.safety_gate


def get_ocr_tool(request: Request) -> OCRTool:
    return request.app.state.ocr_tool


def get_image_analysis_tool(request: Request) -> ImageAnalysisTool:
    return request.app.state.image_analysis_tool


def get_url_fetcher(request: Request) -> SecureUrlFetcher:
    return request.app.state.url_fetcher


def get_url_analyzer_tool(request: Request) -> UrlAnalyzerTool:
    return request.app.state.url_analyzer_tool


def get_url_safety_tool(request: Request) -> UrlSafetyTool:
    return request.app.state.url_safety_tool


def get_stt_tool(request: Request) -> STTTool:
    return request.app.state.stt_tool


def get_audio_forensics_tool(request: Request) -> AudioForensicsTool:
    return request.app.state.audio_forensics_tool


def get_video_media_client(request: Request) -> MetaMediaClient:
    """A SEPARATE MetaMediaClient instance again — sized for video
    (max_upload_size_bytes_video, 100MB) via main.py's lifespan."""
    return request.app.state.video_media_client


def get_video_forensics_tool(request: Request) -> VideoForensicsTool:
    return request.app.state.video_forensics_tool


def get_sender(settings: Settings = Depends(get_settings)) -> WhatsAppSender:
    return WhatsAppSender(
        base_url=settings.whatsapp_graph_base_url,
        phone_number_id=settings.whatsapp_phone_number_id,
        access_token=settings.whatsapp_access_token,
    )


@router.get("")
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
):
    """Meta's one-time webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_webhook_verify_token:
        log_event(logger, logging.INFO, "webhook verification succeeded")
        return PlainTextResponse(content=hub_challenge or "")

    log_event(logger, logging.WARNING, "webhook verification failed", mode=hub_mode)
    return PlainTextResponse(content="Verification failed.", status_code=403)


@dataclass(frozen=True)
class _SuspendedOutcome:
    """Phase 9 — admin "Suspend user" (roadmap §9.1). A tiny, shared
    stand-in for whichever real pipeline outcome type each `_do_work()`
    below would otherwise return — every caller only ever reads
    `outcome.reply_text`, so one shape covers all five pipelines without
    needing each one's own dataclass imported here."""

    reply_text: str


async def _send_reply(sender: WhatsAppSender, message: NormalizedMessage, text: str) -> None:
    try:
        await sender.send_text_message(message.from_phone, message.phone_hash, text)
    except WhatsAppSendError:
        # Already logged inside sender; nothing more to do — this is a
        # background task, there's no request left to return an error on.
        pass


async def _run_text_pipeline_and_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    search_provider: EvidenceSearchProvider,
    redis: Redis,
    settings: Settings,
    sender: WhatsAppSender,
    message: NormalizedMessage,
) -> None:
    """The Phase 2 background task for text messages. Runs the full
    orchestrator (slow: multiple Claude/search calls), so — per
    architecture.md's documented pattern — sends an immediate ack first, then
    the real result once the pipeline finishes. Any unexpected failure here
    must still resolve to a friendly reply, never a silently dropped message
    or a raw error (PRD §38 / decisions.md §15)."""
    response_tool = ResponseGenerationTool()
    language = None

    async def _do_work():
        nonlocal language
        async with session_scope(sessionmaker) as session:
            user = await get_or_create_user(
                session,
                message.from_phone,
                settings.phone_hash_pepper,
                settings.phone_encryption_key,
            )
            language = user.preferred_language
            if user.is_suspended:
                return _SuspendedOutcome(reply_text=response_tool.generate_account_suspended_reply(language))

            await _send_reply(sender, message, response_tool.generate_analyzing_ack(language))

            deps = PipelineDependencies(llm=llm, search_provider=search_provider, redis=redis)
            pipeline = TextPipeline(session, deps, settings)
            return await pipeline.run(
                user=user,
                text=message.text_body or "",
                source_wamid=message.wamid,
                language=language,
            )

    try:
        outcome = await run_with_timeout(_do_work(), settings.max_processing_duration_seconds)
        await _send_reply(sender, message, outcome.reply_text)

    except PipelineTimeoutError:
        log_event(
            logger,
            logging.WARNING,
            "text pipeline timed out",
            phone_hash=message.phone_hash[:8],
            timeout_seconds=settings.max_processing_duration_seconds,
        )
        await _send_reply(sender, message, response_tool.generate_processing_timeout_reply(language))

    except Exception:
        log_event(
            logger,
            logging.ERROR,
            "text pipeline failed unexpectedly",
            phone_hash=message.phone_hash[:8],
        )
        await _send_reply(sender, message, response_tool.generate_processing_error_reply(language))


async def _run_image_pipeline_and_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    search_provider: EvidenceSearchProvider,
    redis: Redis,
    media_client: MetaMediaClient,
    safety_gate: SafetyGate,
    ocr_tool: OCRTool,
    image_analysis_tool: ImageAnalysisTool,
    settings: Settings,
    sender: WhatsAppSender,
    message: NormalizedMessage,
) -> None:
    """Phase 3 background task for image messages — mirrors
    _run_text_pipeline_and_reply exactly (immediate ack, then the real
    result), see docs/whatsapp-integration.md."""
    response_tool = ResponseGenerationTool()
    language = None

    async def _do_work():
        nonlocal language
        async with session_scope(sessionmaker) as session:
            user = await get_or_create_user(
                session,
                message.from_phone,
                settings.phone_hash_pepper,
                settings.phone_encryption_key,
            )
            language = user.preferred_language
            if user.is_suspended:
                return _SuspendedOutcome(reply_text=response_tool.generate_account_suspended_reply(language))

            await _send_reply(sender, message, response_tool.generate_analyzing_image_ack(language))

            deps = ImagePipelineDependencies(
                llm=llm,
                search_provider=search_provider,
                redis=redis,
                media_client=media_client,
                safety_gate=safety_gate,
                ocr_tool=ocr_tool,
                image_analysis_tool=image_analysis_tool,
            )
            pipeline = ImagePipeline(session, deps, settings)
            return await pipeline.run(
                user=user,
                media_id=message.media.media_id if message.media else "",
                source_wamid=message.wamid,
                language=language,
            )

    try:
        outcome = await run_with_timeout(_do_work(), settings.max_processing_duration_seconds)
        await _send_reply(sender, message, outcome.reply_text)

    except PipelineTimeoutError:
        log_event(
            logger,
            logging.WARNING,
            "image pipeline timed out",
            phone_hash=message.phone_hash[:8],
            timeout_seconds=settings.max_processing_duration_seconds,
        )
        await _send_reply(sender, message, response_tool.generate_processing_timeout_reply(language))

    except Exception:
        log_event(
            logger,
            logging.ERROR,
            "image pipeline failed unexpectedly",
            phone_hash=message.phone_hash[:8],
        )
        await _send_reply(sender, message, response_tool.generate_processing_error_reply(language))


async def _run_url_pipeline_and_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    search_provider: EvidenceSearchProvider,
    redis: Redis,
    url_fetcher: SecureUrlFetcher,
    url_analyzer_tool: UrlAnalyzerTool,
    url_safety_tool: UrlSafetyTool,
    settings: Settings,
    sender: WhatsAppSender,
    message: NormalizedMessage,
    url: str,
) -> None:
    """Phase 4 background task for bare-URL text messages — mirrors
    _run_image_pipeline_and_reply exactly (immediate ack, then the real
    result), see docs/whatsapp-integration.md."""
    response_tool = ResponseGenerationTool()
    language = None

    async def _do_work():
        nonlocal language
        async with session_scope(sessionmaker) as session:
            user = await get_or_create_user(
                session,
                message.from_phone,
                settings.phone_hash_pepper,
                settings.phone_encryption_key,
            )
            language = user.preferred_language
            if user.is_suspended:
                return _SuspendedOutcome(reply_text=response_tool.generate_account_suspended_reply(language))

            await _send_reply(sender, message, response_tool.generate_analyzing_url_ack(language))

            deps = UrlPipelineDependencies(
                llm=llm,
                search_provider=search_provider,
                redis=redis,
                url_fetcher=url_fetcher,
                url_analyzer_tool=url_analyzer_tool,
                url_safety_tool=url_safety_tool,
            )
            pipeline = UrlPipeline(session, deps, settings)
            return await pipeline.run(
                user=user,
                url=url,
                source_wamid=message.wamid,
                language=language,
            )

    try:
        outcome = await run_with_timeout(_do_work(), settings.max_processing_duration_seconds)
        await _send_reply(sender, message, outcome.reply_text)

    except PipelineTimeoutError:
        log_event(
            logger,
            logging.WARNING,
            "URL pipeline timed out",
            phone_hash=message.phone_hash[:8],
            timeout_seconds=settings.max_processing_duration_seconds,
        )
        await _send_reply(sender, message, response_tool.generate_processing_timeout_reply(language))

    except Exception:
        log_event(
            logger,
            logging.ERROR,
            "URL pipeline failed unexpectedly",
            phone_hash=message.phone_hash[:8],
        )
        await _send_reply(sender, message, response_tool.generate_processing_error_reply(language))


async def _run_audio_pipeline_and_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    search_provider: EvidenceSearchProvider,
    redis: Redis,
    media_client: MetaMediaClient,
    safety_gate: SafetyGate,
    stt_tool: STTTool,
    audio_forensics_tool: AudioForensicsTool,
    settings: Settings,
    sender: WhatsAppSender,
    message: NormalizedMessage,
) -> None:
    """Phase 5a background task for audio messages — mirrors
    _run_image_pipeline_and_reply exactly (immediate ack, then the real
    result), see docs/whatsapp-integration.md."""
    response_tool = ResponseGenerationTool()
    language = None

    async def _do_work():
        nonlocal language
        async with session_scope(sessionmaker) as session:
            user = await get_or_create_user(
                session,
                message.from_phone,
                settings.phone_hash_pepper,
                settings.phone_encryption_key,
            )
            language = user.preferred_language
            if user.is_suspended:
                return _SuspendedOutcome(reply_text=response_tool.generate_account_suspended_reply(language))

            await _send_reply(sender, message, response_tool.generate_analyzing_audio_ack(language))

            deps = AudioPipelineDependencies(
                llm=llm,
                search_provider=search_provider,
                redis=redis,
                media_client=media_client,
                safety_gate=safety_gate,
                stt_tool=stt_tool,
                audio_forensics_tool=audio_forensics_tool,
            )
            pipeline = AudioPipeline(session, deps, settings)
            return await pipeline.run(
                user=user,
                media_id=message.media.media_id if message.media else "",
                source_wamid=message.wamid,
                language=language,
            )

    try:
        outcome = await run_with_timeout(_do_work(), settings.max_processing_duration_seconds)
        await _send_reply(sender, message, outcome.reply_text)

    except PipelineTimeoutError:
        log_event(
            logger,
            logging.WARNING,
            "audio pipeline timed out",
            phone_hash=message.phone_hash[:8],
            timeout_seconds=settings.max_processing_duration_seconds,
        )
        await _send_reply(sender, message, response_tool.generate_processing_timeout_reply(language))

    except Exception:
        log_event(
            logger,
            logging.ERROR,
            "audio pipeline failed unexpectedly",
            phone_hash=message.phone_hash[:8],
        )
        await _send_reply(sender, message, response_tool.generate_processing_error_reply(language))


async def _run_video_pipeline_and_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    search_provider: EvidenceSearchProvider,
    redis: Redis,
    media_client: MetaMediaClient,
    safety_gate: SafetyGate,
    stt_tool: STTTool,
    ocr_tool: OCRTool,
    video_forensics_tool: VideoForensicsTool,
    settings: Settings,
    sender: WhatsAppSender,
    message: NormalizedMessage,
) -> None:
    """Phase 5b background task for video messages — mirrors
    _run_audio_pipeline_and_reply exactly (immediate ack, then the real
    result), see docs/whatsapp-integration.md. Reuses the EXACT SAME
    ocr_tool (app.state.ocr_tool) ImagePipeline uses — no second OCR engine."""
    response_tool = ResponseGenerationTool()
    language = None

    async def _do_work():
        nonlocal language
        async with session_scope(sessionmaker) as session:
            user = await get_or_create_user(
                session,
                message.from_phone,
                settings.phone_hash_pepper,
                settings.phone_encryption_key,
            )
            language = user.preferred_language
            if user.is_suspended:
                return _SuspendedOutcome(reply_text=response_tool.generate_account_suspended_reply(language))

            await _send_reply(sender, message, response_tool.generate_analyzing_video_ack(language))

            deps = VideoPipelineDependencies(
                llm=llm,
                search_provider=search_provider,
                redis=redis,
                media_client=media_client,
                safety_gate=safety_gate,
                stt_tool=stt_tool,
                ocr_tool=ocr_tool,
                video_forensics_tool=video_forensics_tool,
            )
            pipeline = VideoPipeline(session, deps, settings)
            return await pipeline.run(
                user=user,
                media_id=message.media.media_id if message.media else "",
                source_wamid=message.wamid,
                language=language,
            )

    try:
        outcome = await run_with_timeout(_do_work(), settings.max_processing_duration_seconds)
        await _send_reply(sender, message, outcome.reply_text)

    except PipelineTimeoutError:
        log_event(
            logger,
            logging.WARNING,
            "video pipeline timed out",
            phone_hash=message.phone_hash[:8],
            timeout_seconds=settings.max_processing_duration_seconds,
        )
        await _send_reply(sender, message, response_tool.generate_processing_timeout_reply(language))

    except Exception:
        log_event(
            logger,
            logging.ERROR,
            "video pipeline failed unexpectedly",
            phone_hash=message.phone_hash[:8],
        )
        await _send_reply(sender, message, response_tool.generate_processing_error_reply(language))


@router.post("")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    sender: WhatsAppSender = Depends(get_sender),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_db_sessionmaker),
    llm: LLMClient = Depends(get_llm_client),
    search_provider: EvidenceSearchProvider = Depends(get_search_provider),
    media_client: MetaMediaClient = Depends(get_media_client),
    safety_gate: SafetyGate = Depends(get_safety_gate),
    ocr_tool: OCRTool = Depends(get_ocr_tool),
    image_analysis_tool: ImageAnalysisTool = Depends(get_image_analysis_tool),
    url_fetcher: SecureUrlFetcher = Depends(get_url_fetcher),
    url_analyzer_tool: UrlAnalyzerTool = Depends(get_url_analyzer_tool),
    url_safety_tool: UrlSafetyTool = Depends(get_url_safety_tool),
    stt_tool: STTTool = Depends(get_stt_tool),
    audio_forensics_tool: AudioForensicsTool = Depends(get_audio_forensics_tool),
    audio_media_client: MetaMediaClient = Depends(get_audio_media_client),
    video_media_client: MetaMediaClient = Depends(get_video_media_client),
    video_forensics_tool: VideoForensicsTool = Depends(get_video_forensics_tool),
):
    raw_body = await verify_signature_and_get_body(request, settings)

    try:
        payload = json.loads(raw_body)
        envelope = WebhookEnvelope.model_validate(payload)
    except (json.JSONDecodeError, ValueError):
        # Signature was valid but the body wasn't well-formed JSON we
        # recognize. Acknowledge with 200 (nothing a retry would fix) and
        # move on — never a raw error back to Meta.
        log_event(logger, logging.WARNING, "webhook payload could not be parsed")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    messages = parse_normalized_messages(envelope, settings.phone_hash_pepper)

    idempotency = IdempotencyGuard(redis, settings.idempotency_ttl_seconds)
    rate_limiter = RateLimiter(redis, settings.rate_limit_per_user_per_minute)

    for message in messages:
        if await idempotency.is_duplicate(message.wamid):
            log_event(logger, logging.INFO, "duplicate wamid skipped", phone_hash=message.phone_hash[:8])
            continue

        rate_result = await rate_limiter.check_and_increment(message.phone_hash)
        if not rate_result.allowed:
            log_event(
                logger,
                logging.WARNING,
                "rate limit exceeded",
                phone_hash=message.phone_hash[:8],
                count=rate_result.current_count,
                limit=rate_result.limit,
            )
            background_tasks.add_task(_send_reply, sender, message, build_rate_limit_reply())
            continue

        log_event(
            logger,
            logging.INFO,
            "message received",
            phone_hash=message.phone_hash[:8],
            wamid=message.wamid,
            message_type=message.message_type,
        )

        bare_url = extract_bare_url(message.text_body) if message.message_type == "text" else None

        if bare_url is not None:
            # Phase 4: a text message whose entire body is a single URL
            # routes to the URL Analyzer + URL Safety pipeline instead of
            # plain claim extraction — see app/agent/url_detection.py.
            background_tasks.add_task(
                _run_url_pipeline_and_reply,
                sessionmaker,
                llm,
                search_provider,
                redis,
                url_fetcher,
                url_analyzer_tool,
                url_safety_tool,
                settings,
                sender,
                message,
                bare_url,
            )
        elif message.message_type == "text":
            # Phase 2: real fact-checking pipeline, not an echo.
            background_tasks.add_task(
                _run_text_pipeline_and_reply,
                sessionmaker,
                llm,
                search_provider,
                redis,
                settings,
                sender,
                message,
            )
        elif message.message_type == "image":
            # Phase 3: real safety-gate + OCR pipeline, not "not analyzed yet".
            background_tasks.add_task(
                _run_image_pipeline_and_reply,
                sessionmaker,
                llm,
                search_provider,
                redis,
                media_client,
                safety_gate,
                ocr_tool,
                image_analysis_tool,
                settings,
                sender,
                message,
            )
        elif message.message_type == "audio":
            # Phase 5a: real transcription + fact-check pipeline, not "not
            # analyzed yet".
            background_tasks.add_task(
                _run_audio_pipeline_and_reply,
                sessionmaker,
                llm,
                search_provider,
                redis,
                audio_media_client,
                safety_gate,
                stt_tool,
                audio_forensics_tool,
                settings,
                sender,
                message,
            )
        elif message.message_type == "video":
            # Phase 5b: real transcription + OCR + multimodal fact-check
            # pipeline, not "not analyzed yet".
            background_tasks.add_task(
                _run_video_pipeline_and_reply,
                sessionmaker,
                llm,
                search_provider,
                redis,
                video_media_client,
                safety_gate,
                stt_tool,
                ocr_tool,
                video_forensics_tool,
                settings,
                sender,
                message,
            )
        else:
            # document/sticker/unsupported: unchanged from Phase 1.
            background_tasks.add_task(_send_reply, sender, message, build_reply_text(message))

    return JSONResponse(status_code=200, content={"status": "received", "message_count": len(messages)})
