"""
Application configuration, loaded from environment variables / a .env file.

Phase 1 declared only the WhatsApp webhook foundation's settings. Phase 2
adds the database, Claude/search-provider, cost-control, and phone-encryption
settings needed by the text fact-checking pipeline (docs/decisions.md).
Settings for phases beyond 2 (media forensics, safety gate, JWT/dashboard
auth, etc.) still remain undeclared here — see the repo-root .env.example
for the full placeholder set and which phase each belongs to.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Supports running uvicorn from either the repo root or backend/.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ==== Core ====
    env: str = Field(default="development", alias="ENV")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    # ==== WhatsApp Business Cloud API (Meta) ====
    whatsapp_app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    whatsapp_webhook_verify_token: str = Field(default="", alias="WHATSAPP_WEBHOOK_VERIFY_TOKEN")
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_business_account_id: str = Field(default="", alias="WHATSAPP_BUSINESS_ACCOUNT_ID")
    whatsapp_api_version: str = Field(default="v21.0", alias="WHATSAPP_API_VERSION")

    # ==== Redis ====
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # ==== Security (decisions.md §7) ====
    # Server-side pepper for the sender phone-number HMAC. NOT the WhatsApp app secret.
    # Phase 1 never persists a phone number anywhere — this hash only keys short-lived
    # Redis rate-limit/idempotency-adjacent lookups, and is what appears in logs.
    phone_hash_pepper: str = Field(default="", alias="PHONE_HASH_PEPPER")

    # ==== Rate limiting & idempotency (decisions.md §1, §12) ====
    rate_limit_per_user_per_minute: int = Field(default=10, alias="RATE_LIMIT_PER_USER_PER_MINUTE")
    idempotency_ttl_seconds: int = Field(default=86400, alias="IDEMPOTENCY_TTL_SECONDS")

    # ==== Database (Phase 2) ====
    database_url: str = Field(
        default="postgresql+asyncpg://sathyascan:sathyascan@localhost:5432/sathyascan",
        alias="DATABASE_URL",
    )

    # ==== Phone encryption (decisions.md §7 — see core/encryption.py for the
    # documented KMS-vs-local-key scope note) ====
    phone_encryption_key: str = Field(default="", alias="PHONE_ENCRYPTION_KEY")

    # ==== Claude / Anthropic (decisions.md agent-architecture.md) ====
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5", alias="ANTHROPIC_MODEL")

    # ==== Evidence search provider ====
    search_api_key: str = Field(default="", alias="SEARCH_API_KEY")
    search_api_provider: str = Field(default="tavily", alias="SEARCH_API_PROVIDER")

    # ==== Per-analysis & global LLM/evidence-search budget (decisions.md §1/§1A) ====
    max_evidence_searches_per_analysis: int = Field(default=5, alias="MAX_EVIDENCE_SEARCHES_PER_ANALYSIS")
    max_llm_tool_calls_per_analysis: int = Field(default=10, alias="MAX_LLM_TOOL_CALLS_PER_ANALYSIS")
    daily_spend_circuit_breaker_usd: float = Field(default=50.0, alias="DAILY_SPEND_CIRCUIT_BREAKER_USD")
    circuit_breaker_queue_on_trip: bool = Field(default=True, alias="CIRCUIT_BREAKER_QUEUE_ON_TRIP")
    estimated_cost_per_search_usd: float = Field(default=0.01, alias="ESTIMATED_COST_PER_SEARCH_USD")
    estimated_cost_per_llm_call_usd: float = Field(default=0.02, alias="ESTIMATED_COST_PER_LLM_CALL_USD")
    global_llm_search_concurrency_limit: int = Field(default=20, alias="GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT")

    # ==== Evidence-tier policy (decisions.md §3/§5) ====
    min_concurring_tier2_sources: int = Field(default=2, alias="MIN_CONCURRING_TIER2_SOURCES")
    high_impact_categories: str = Field(default="elections,gov_scheme", alias="HIGH_IMPACT_CATEGORIES")

    # ==== Duplicate-content detection (decisions.md §12) ====
    duplicate_content_reuse_enabled: bool = Field(default=True, alias="DUPLICATE_CONTENT_REUSE_ENABLED")
    duplicate_content_reuse_window_hours: int = Field(default=72, alias="DUPLICATE_CONTENT_REUSE_WINDOW_HOURS")

    # ==== SSRF protection (decisions.md §15 — wired to the media downloader
    # in Phase 3, see app/media/downloader.py) ====
    ssrf_blocklist_enabled: bool = Field(default=True, alias="SSRF_BLOCKLIST_ENABLED")

    # ==== Media handling (Phase 3, decisions.md §12/§15) ====
    max_upload_size_bytes_image: int = Field(default=10_485_760, alias="MAX_UPLOAD_SIZE_BYTES_IMAGE")
    max_image_pixels: int = Field(default=40_000_000, alias="MAX_IMAGE_PIXELS")  # decompression-bomb guard
    media_download_timeout_seconds: float = Field(default=15.0, alias="MEDIA_DOWNLOAD_TIMEOUT_SECONDS")

    # ==== Safety gate (decisions.md §6 — vendor intentionally not decided,
    # pending mandatory legal/professional review; see app/agent/safety_gate.py) ====
    safety_gate_enabled: bool = Field(default=True, alias="SAFETY_GATE_ENABLED")
    safety_gate_provider: str = Field(default="", alias="SAFETY_GATE_PROVIDER")
    safety_gate_api_key: str = Field(default="", alias="SAFETY_GATE_API_KEY")

    # ==== OCR (decisions.md §11 — still no BENCHMARKED engine; see
    # app/agent/tools/ocr.py and docs/risks-and-open-questions.md) ====
    # "" (default, Null stub) | "claude_vision"
    # `claude_vision` reuses ANTHROPIC_API_KEY — it needs no separate
    # OCR_API_KEY, because the backend is the same Anthropic model this
    # deployment already uses (app/integrations/vision_ocr_client.py).
    ocr_provider: str = Field(default="", alias="OCR_PROVIDER")
    # Retained for a future dedicated OCR vendor that needs its own key;
    # unused by claude_vision.
    ocr_api_key: str = Field(default="", alias="OCR_API_KEY")

    # ==== Image analysis / AI-generation detection (decisions.md §2 — no
    # third-party provider wired this phase, see app/agent/tools/image_analysis.py) ====
    image_analysis_provider: str = Field(default="", alias="IMAGE_ANALYSIS_PROVIDER")
    image_analysis_api_key: str = Field(default="", alias="IMAGE_ANALYSIS_API_KEY")

    # ==== URL Analyzer / URL Safety Module (Phase 4, decisions.md §3/§15) ====
    url_fetch_timeout_seconds: float = Field(default=10.0, alias="URL_FETCH_TIMEOUT_SECONDS")
    max_url_fetch_size_bytes: int = Field(default=5_242_880, alias="MAX_URL_FETCH_SIZE_BYTES")  # 5 MB
    max_url_redirects: int = Field(default=5, alias="MAX_URL_REDIRECTS")
    url_fetch_user_agent: str = Field(
        default="SathyaScanBot/1.0 (+https://sathyascan.example/bot)", alias="URL_FETCH_USER_AGENT"
    )
    max_url_content_length_chars: int = Field(default=20_000, alias="MAX_URL_CONTENT_LENGTH_CHARS")
    url_robots_txt_check_enabled: bool = Field(default=True, alias="URL_ROBOTS_TXT_CHECK_ENABLED")
    whois_lookup_timeout_seconds: float = Field(default=5.0, alias="WHOIS_LOOKUP_TIMEOUT_SECONDS")

    # ==== Threat intelligence (Safe Browsing-style, decisions.md — explicitly
    # OPTIONAL per phased-plan.md's Phase 4 scope; no provider wired, see
    # app/agent/tools/url_safety.py) ====
    threat_intel_provider: str = Field(default="", alias="THREAT_INTEL_PROVIDER")
    threat_intel_api_key: str = Field(default="", alias="THREAT_INTEL_API_KEY")

    # ==== Wall-clock processing timeout (decisions.md §12/§15) — documented
    # since Phase 2 but only actually wired to every pipeline in Phase 5, see
    # app/core/pipeline_timeout.py. Applies uniformly to Text/Image/Url/
    # Audio(/Video) pipelines, not just the new heavy ones. ====
    max_processing_duration_seconds: float = Field(default=120.0, alias="MAX_PROCESSING_DURATION_SECONDS")

    # ==== Heavy-media concurrency (decisions.md §12 — originally named for a
    # Celery "heavy" queue that was never built; Phases 1-4 run everything on
    # FastAPI BackgroundTasks + GlobalConcurrencyGuard instead, so this is
    # currently UNUSED — audio/video reuse global_llm_search_concurrency_limit
    # like every other pipeline, not a separate pool. Kept here as a
    # documented placeholder for a future real task-queue split, not
    # silently dropped.) ====
    global_heavy_queue_concurrency_limit: int = Field(default=5, alias="GLOBAL_HEAVY_QUEUE_CONCURRENCY_LIMIT")

    # ==== Audio handling (Phase 5, decisions.md §12/§15) ====
    max_upload_size_bytes_audio: int = Field(default=26_214_400, alias="MAX_UPLOAD_SIZE_BYTES_AUDIO")  # 25 MB
    max_audio_duration_seconds: float = Field(default=600.0, alias="MAX_AUDIO_DURATION_SECONDS")  # 10 min

    # ==== Speech-to-Text (decisions.md §11 — no engine BENCHMARKED yet, same
    # sequencing rule as OCR; see app/agent/tools/speech_to_text.py). Phase
    # "A-real": a genuine provider implementation now exists
    # (app/integrations/speech_to_text_client.py, Sarvam AI) but stays
    # OPT-IN — NullSpeechToTextProvider remains main.py's default unless
    # SPEECH_TO_TEXT_PROVIDER + SPEECH_TO_TEXT_API_KEY are both set. This
    # does not skip decisions.md §11's benchmark-before-lock-in rule: no
    # engine is being locked in here, just made available to opt into. ====
    speech_to_text_provider: str = Field(default="", alias="SPEECH_TO_TEXT_PROVIDER")  # "" | "sarvam"
    speech_to_text_api_key: str = Field(default="", alias="SPEECH_TO_TEXT_API_KEY")
    speech_to_text_model: str = Field(default="saaras:v3", alias="SPEECH_TO_TEXT_MODEL")
    speech_to_text_timeout_seconds: float = Field(default=30.0, alias="SPEECH_TO_TEXT_TIMEOUT_SECONDS")

    # ==== Audio forensics / AI-voice detection (decisions.md §2 — never
    # self-hosted; see app/agent/tools/audio_forensics.py). A genuine
    # provider implementation now exists (Resemble AI Detect,
    # app/integrations/media_forensics_client.py) but stays OPT-IN —
    # NullAudioForensicsProvider remains main.py's default unless
    # AUDIO_FORENSICS_PROVIDER + AUDIO_FORENSICS_API_KEY are both set. Per-
    # unit cost is real (~$0.035/sec on Resemble's entry Flex tier as of
    # this writing) — a confirmed, deliberate decision to keep this
    # opt-in-only rather than a default-on spend. ====
    audio_forensics_provider: str = Field(default="", alias="AUDIO_FORENSICS_PROVIDER")  # "" | "resemble"
    audio_forensics_api_key: str = Field(default="", alias="AUDIO_FORENSICS_API_KEY")
    audio_forensics_timeout_seconds: float = Field(default=30.0, alias="AUDIO_FORENSICS_TIMEOUT_SECONDS")

    # ==== Video handling (Phase 5, decisions.md §12/§15) — see
    # app/media/{video_validation,frame_extractor,audio_extractor}.py ====
    max_upload_size_bytes_video: int = Field(default=104_857_600, alias="MAX_UPLOAD_SIZE_BYTES_VIDEO")  # 100 MB
    max_video_duration_seconds: float = Field(default=300.0, alias="MAX_VIDEO_DURATION_SECONDS")  # 5 min
    max_video_frames: int = Field(default=8, alias="MAX_VIDEO_FRAMES")
    video_frame_sampling_interval_seconds: float = Field(
        default=5.0, alias="VIDEO_FRAME_SAMPLING_INTERVAL_SECONDS"
    )

    # ==== Video forensics / AI-generated-video detection (decisions.md §2 —
    # never self-hosted; see app/agent/tools/video_forensics.py). Same
    # opt-in pattern as audio forensics above (Resemble AI Detect) —
    # NullVideoForensicsProvider remains the default; per-unit cost is real
    # (~$0.07/sec on Resemble's entry Flex tier as of this writing). ====
    video_forensics_provider: str = Field(default="", alias="VIDEO_FORENSICS_PROVIDER")  # "" | "resemble"
    video_forensics_api_key: str = Field(default="", alias="VIDEO_FORENSICS_API_KEY")
    video_forensics_timeout_seconds: float = Field(default=60.0, alias="VIDEO_FORENSICS_TIMEOUT_SECONDS")

    # ==== Dashboard JWT auth (Phase 6, decisions.md §7/api-design.md — see
    # app/core/jwt_auth.py). JWT_SECRET was a placeholder in .env.example
    # since Phase 1 planning; wired to real use for the first time here.
    # Empty-string default fails loud (jwt_auth.create_access_token raises)
    # rather than silently signing tokens with a guessable secret. ====
    jwt_secret: str = Field(default="", alias="JWT_SECRET")
    jwt_access_token_ttl_seconds: int = Field(default=3600, alias="JWT_ACCESS_TOKEN_TTL_SECONDS")  # 1h

    # ==== WhatsApp OTP dashboard login (Phase 6, api-design.md — see
    # app/api/v1/auth.py). Reuses the existing WhatsAppSender; production use
    # requires a pre-approved Meta message template for the proactive send
    # (decisions.md §4 — see app/api/v1/auth.py's module docstring). ====
    otp_length: int = Field(default=6, alias="OTP_LENGTH")
    otp_ttl_seconds: int = Field(default=300, alias="OTP_TTL_SECONDS")  # 5 min
    otp_max_attempts: int = Field(default=5, alias="OTP_MAX_ATTEMPTS")
    otp_request_cooldown_seconds: int = Field(default=60, alias="OTP_REQUEST_COOLDOWN_SECONDS")

    # ==== Privacy Mode / retention (decisions.md §8, database-schema.md —
    # documented since Phase 3 planning, wired to a real purge job for the
    # first time in Phase 6; see app/agent/retention.py) ====
    privacy_mode_default: bool = Field(default=True, alias="PRIVACY_MODE_DEFAULT")
    privacy_mode_media_ttl_minutes: int = Field(default=15, alias="PRIVACY_MODE_MEDIA_TTL_MINUTES")
    privacy_mode_analysis_ttl_hours: int = Field(default=48, alias="PRIVACY_MODE_ANALYSIS_TTL_HOURS")
    standard_retention_months: int = Field(default=24, alias="STANDARD_RETENTION_MONTHS")
    retention_purge_interval_seconds: float = Field(
        default=300.0, alias="RETENTION_PURGE_INTERVAL_SECONDS"
    )  # in-process asyncio periodic task cadence, see main.py lifespan

    # ==== Phase 7 — dashboard API rate limiting (decisions.md §12/§15: "Per-
    # user rate limiting... already designed" + "Rate limiting verified (per
    # decision #12)" as a release-gate item). Reuses app/core/rate_limit.py's
    # RateLimiter — the same fixed-window mechanism Phase 1 already built for
    # the webhook — keyed by user_id instead of phone_hash for authenticated
    # routes, and by client IP for the pre-auth OTP-start endpoint (bounds
    # real WhatsApp-send cost exposure: without this, a caller could trigger
    # unlimited OTP sends — a real per-message cost in production — by
    # cycling through phone numbers faster than any one number's own
    # cooldown). See app/api/v1/deps.py / app/api/v1/routers/auth.py. ====
    dashboard_rate_limit_per_user_per_minute: int = Field(
        default=60, alias="DASHBOARD_RATE_LIMIT_PER_USER_PER_MINUTE"
    )
    otp_start_rate_limit_per_ip_per_minute: int = Field(
        default=10, alias="OTP_START_RATE_LIMIT_PER_IP_PER_MINUTE"
    )
    # Content-Length ceiling for /api/v1/* JSON bodies (app/core/http_hardening.py).
    # Every dashboard payload is small (a phone number, an OTP code, a
    # settings toggle) — 64KB is generous headroom, not a tight fit.
    dashboard_max_request_body_bytes: int = Field(
        default=65_536, alias="DASHBOARD_MAX_REQUEST_BODY_BYTES"
    )

    # ==== Phase 8 — "Check This Tomorrow" (decisions.md §14). Backend
    # scheduling/re-check infrastructure only — proactive WhatsApp SEND
    # stays blocked regardless of these values (app/agent/notifications.py).
    # See app/agent/scheduled_check_runner.py / routers/scheduled_checks.py. ====
    scheduled_check_default_delay_hours: int = Field(default=24, alias="SCHEDULED_CHECK_DEFAULT_DELAY_HOURS")
    scheduled_check_execution_interval_seconds: float = Field(
        default=300.0, alias="SCHEDULED_CHECK_EXECUTION_INTERVAL_SECONDS"
    )  # in-process asyncio periodic task cadence, same pattern as retention purge

    # ==== Phase 8 — Explore rollup (app/agent/explore_rollup.py). Reuses
    # the same in-process asyncio periodic task pattern, not a new scheduler. ====
    explore_rollup_interval_seconds: float = Field(default=600.0, alias="EXPLORE_ROLLUP_INTERVAL_SECONDS")
    explore_rollup_lookback_hours: int = Field(default=720, alias="EXPLORE_ROLLUP_LOOKBACK_HOURS")  # 30 days
    # Public, unauthenticated route — per-IP rate limiting (api-design.md:
    # "per-user for authenticated routes, per-IP for public ones").
    explore_rate_limit_per_ip_per_minute: int = Field(default=60, alias="EXPLORE_RATE_LIMIT_PER_IP_PER_MINUTE")


    @property
    def high_impact_categories_set(self) -> set[str]:
        return {c.strip() for c in self.high_impact_categories.split(",") if c.strip()}

    @property
    def whatsapp_graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.whatsapp_api_version}"

    @property
    def is_development(self) -> bool:
        return self.env.lower() in {"development", "dev", "local", "test"}


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Tests override via dependency injection, not by
    mutating this — see tests/conftest.py."""
    return Settings()
