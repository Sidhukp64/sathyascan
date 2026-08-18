"""
Video Forensics tool — a SEPARATE, independent question from claim veracity
(the user's explicit Phase 5 instruction: "Never confuse these two"). This
tool answers "does this video appear AI-generated/deepfaked/manipulated?",
never "is the claim spoken/shown in it true?" — that question is answered
entirely by the existing claim/evidence/classification pipeline
(app/agent/claim_pipeline_shared.py), untouched by this module's result.

Identical design to app/agent/tools/audio_forensics.py — see that module's
docstring for the full rationale (decisions.md §2: third-party APIs only,
never self-hosted; hedged language; mandatory provider/version display
fields). No third-party video-forensics provider is wired this phase — a
confirmed scope decision (no new paid external API, no credentials
available), not an oversight. NullVideoForensicsProvider always reports
PROVIDER_UNAVAILABLE and NEVER guesses.

Shares the exact same status vocabulary as AudioForensicsStatus (see
app/i18n/templates.py's MEDIA_FORENSICS_LABELS, which renders both) —
kept as a distinct class rather than reusing AudioForensicsStatus so the
two modalities can diverge independently later (e.g. video forensics may
eventually need per-frame findings that audio never will) without a shared
enum coupling them.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class VideoForensicsStatus(str, Enum):
    AI_GENERATED_LIKELY = "ai_generated_likely"
    AI_GENERATED_UNLIKELY = "ai_generated_unlikely"
    MANIPULATED_LIKELY = "manipulated_likely"
    AUTHENTICITY_UNCERTAIN = "authenticity_uncertain"
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # no provider configured — the expected Phase 5 state
    FAILED = "failed"  # provider configured but raised unexpectedly — see module docstring


@dataclass(frozen=True)
class VideoForensicsResult:
    status: VideoForensicsStatus
    ai_generated_probability: float | None = None  # NEVER shown as certainty (PRD §39) even when present
    manipulation_score: float | None = None
    provider_name: str | None = None
    model_version: str | None = None


class VideoForensicsProviderError(Exception):
    pass


class VideoForensicsProvider(Protocol):
    async def analyze(self, video_bytes: bytes, mime_type: str) -> VideoForensicsResult: ...


class NullVideoForensicsProvider:
    """Stub — see module docstring. Never fabricates a probability or a
    likely/unlikely verdict."""

    provider_name = "null_stub_no_provider_configured"

    async def analyze(self, video_bytes: bytes, mime_type: str) -> VideoForensicsResult:
        log_event(
            logger,
            logging.INFO,
            "video forensics: no provider configured — AI-generation detection unavailable this request",
        )
        return VideoForensicsResult(status=VideoForensicsStatus.PROVIDER_UNAVAILABLE, provider_name=self.provider_name)


class VideoForensicsTool:
    def __init__(self, provider: VideoForensicsProvider) -> None:
        self._provider = provider

    async def analyze(self, video_bytes: bytes, mime_type: str) -> VideoForensicsResult:
        # Phase 9 — provider health tracking (roadmap §9.5); see
        # app/core/provider_health.py's docstring.
        started_at = time.monotonic()
        try:
            result = await self._provider.analyze(video_bytes, mime_type)
            provider_health.record_success("video_forensics", (time.monotonic() - started_at) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            provider_health.record_failure("video_forensics", type(exc).__name__)
            log_event(logger, logging.ERROR, "video forensics provider raised unexpectedly")
            return VideoForensicsResult(
                status=VideoForensicsStatus.FAILED, provider_name=getattr(self._provider, "provider_name", None)
            )
