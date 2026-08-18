"""
Image Analyzer tool — agent-architecture.md's contract:
{media_ref, mime_type} -> {ai_generated_probability, manipulation_score,
manipulation_regions[], metadata}, with provider_name/model_version
mandatory whenever a real result exists (decisions.md §2).

Per the user's explicit Phase 3 scope (confirmed): no third-party
media-forensics API is wired this phase — that would be a new external API
dependency needing real credentials this environment doesn't have, and the
scope instruction was to avoid unnecessary new external APIs and expensive
multimodal calls. NullImageForensicsProvider always reports UNAVAILABLE.

Critically: an unavailable/stubbed image-analysis result must NEVER be
silently treated as evidence, and must never by itself force
insufficient_evidence either — per agent-architecture.md's Multimodal
Evidence Fusion policy, a missing modality is noted transparently and only
blocks the result if no other modality (here: OCR'd text evidence) can meet
the required evidence tier on its own. See app/agent/image_pipeline.py.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class ImageAnalysisStatus(str, Enum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"  # no provider configured — the expected Phase 3 state
    FAILED = "failed"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True)
class ImageAnalysisResult:
    status: ImageAnalysisStatus
    ai_generated_probability: float | None = None  # NEVER shown as certainty (PRD §39) even when present
    manipulation_score: float | None = None
    provider_name: str | None = None
    model_version: str | None = None


class ImageAnalysisProviderError(Exception):
    pass


class ImageAnalysisProvider(Protocol):
    async def analyze(self, image_bytes: bytes, mime_type: str) -> ImageAnalysisResult: ...


class NullImageForensicsProvider:
    """Stub — see module docstring. Never fabricates a probability."""

    provider_name = "null_stub_no_provider_configured"

    async def analyze(self, image_bytes: bytes, mime_type: str) -> ImageAnalysisResult:
        log_event(
            logger,
            logging.INFO,
            "image forensics: no provider configured — visual authenticity analysis unavailable this request",
        )
        return ImageAnalysisResult(status=ImageAnalysisStatus.UNAVAILABLE, provider_name=self.provider_name)


class ImageAnalysisTool:
    def __init__(self, provider: ImageAnalysisProvider) -> None:
        self._provider = provider

    async def analyze(self, image_bytes: bytes, mime_type: str) -> ImageAnalysisResult:
        # Phase 9 — provider health tracking (roadmap §9.5); see
        # app/core/provider_health.py's docstring.
        started_at = time.monotonic()
        try:
            result = await self._provider.analyze(image_bytes, mime_type)
            provider_health.record_success("image_analysis", (time.monotonic() - started_at) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            provider_health.record_failure("image_analysis", type(exc).__name__)
            log_event(logger, logging.ERROR, "image analysis provider raised unexpectedly")
            return ImageAnalysisResult(status=ImageAnalysisStatus.FAILED, provider_name=getattr(self._provider, "provider_name", None))
