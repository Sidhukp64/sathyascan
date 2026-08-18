"""
Audio Forensics tool — a SEPARATE, independent question from claim veracity
(the user's explicit Phase 5 instruction: "Never confuse these two"). This
tool answers "does this audio appear AI-generated/synthetic/manipulated?",
never "is the claim spoken in it true?" — that question is answered entirely
by the existing claim/evidence/classification pipeline
(app/agent/claim_pipeline_shared.py), untouched by this module's result.

decisions.md §2 applies directly here, extended from images to audio:
third-party detection APIs only, NEVER a self-hosted forensics model;
hedged language always (never "is AI-generated", always "likely"/"possibly");
provider_name + model_version are MANDATORY display fields whenever a real
result exists. No third-party audio-forensics provider is wired this phase —
a confirmed scope decision (no new paid external API, no credentials
available), not an oversight. NullAudioForensicsProvider always reports
PROVIDER_UNAVAILABLE and NEVER guesses; per the user's explicit instruction,
the WhatsApp reply must say "AI-generation detection unavailable" verbatim
in this case, not a fabricated probability.

Status values mirror the user's explicit Phase 5 spec plus one addition
(FAILED) for the wrapper's own unexpected-exception path, matching the exact
convention every other Tool wrapper in this codebase already uses
(OCRTool/ImageAnalysisTool both distinguish "provider not configured" from
"provider configured but crashed") — not one of the five states the user
named, but necessary for the same honest-error-handling reason those two
tools have it.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class AudioForensicsStatus(str, Enum):
    AI_GENERATED_LIKELY = "ai_generated_likely"
    AI_GENERATED_UNLIKELY = "ai_generated_unlikely"
    MANIPULATED_LIKELY = "manipulated_likely"
    AUTHENTICITY_UNCERTAIN = "authenticity_uncertain"
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # no provider configured — the expected Phase 5 state
    FAILED = "failed"  # provider configured but raised unexpectedly — see module docstring


@dataclass(frozen=True)
class AudioForensicsResult:
    status: AudioForensicsStatus
    ai_generated_probability: float | None = None  # NEVER shown as certainty (PRD §39) even when present
    manipulation_score: float | None = None
    provider_name: str | None = None
    model_version: str | None = None


class AudioForensicsProviderError(Exception):
    pass


class AudioForensicsProvider(Protocol):
    async def analyze(self, audio_bytes: bytes, mime_type: str) -> AudioForensicsResult: ...


class NullAudioForensicsProvider:
    """Stub — see module docstring. Never fabricates a probability or a
    likely/unlikely verdict."""

    provider_name = "null_stub_no_provider_configured"

    async def analyze(self, audio_bytes: bytes, mime_type: str) -> AudioForensicsResult:
        log_event(
            logger,
            logging.INFO,
            "audio forensics: no provider configured — AI-generation detection unavailable this request",
        )
        return AudioForensicsResult(status=AudioForensicsStatus.PROVIDER_UNAVAILABLE, provider_name=self.provider_name)


class AudioForensicsTool:
    def __init__(self, provider: AudioForensicsProvider) -> None:
        self._provider = provider

    async def analyze(self, audio_bytes: bytes, mime_type: str) -> AudioForensicsResult:
        # Phase 9 — provider health tracking (roadmap §9.5); see
        # app/core/provider_health.py's docstring.
        started_at = time.monotonic()
        try:
            result = await self._provider.analyze(audio_bytes, mime_type)
            provider_health.record_success("audio_forensics", (time.monotonic() - started_at) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            provider_health.record_failure("audio_forensics", type(exc).__name__)
            log_event(logger, logging.ERROR, "audio forensics provider raised unexpectedly")
            return AudioForensicsResult(
                status=AudioForensicsStatus.FAILED, provider_name=getattr(self._provider, "provider_name", None)
            )
