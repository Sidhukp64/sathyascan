"""
OCR tool — agent-architecture.md's OCR Engine contract:
{image_ref, language_hint} -> {extracted_text, per_region_confidence[], detected_script}
(here as structured output including provider_name/model_version, per
decisions.md §2's mandatory-display-fields precedent, applied consistently
to every AI-provider-backed result.)

decisions.md §11 locks a hard sequencing rule: benchmarking happens BEFORE
final engine selection. No OCR provider credentials and no curated
Malayalam/Tamil/Hindi/English benchmark image set exist in this build
environment, so no engine has been benchmark-selected — this is a known,
flagged gap (see docs/risks-and-open-questions.md), not a silent pass.
NullOCRProvider is the only provider wired; a real, benchmarked engine is a
drop-in replacement behind the same OCRProvider interface.

OCR failure is modeled explicitly and must never silently collapse to "no
text found" — five distinct states, matching the user's exact requirement:
  success | no_text_found | failed | provider_unavailable | limit_reached
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class OCRStatus(str, Enum):
    SUCCESS = "success"  # ran fine; extracted_text may still be "" if the
    # image genuinely has no legible text — see NO_TEXT_FOUND for the
    # explicit low-confidence-scan variant a real provider may report distinctly.
    NO_TEXT_FOUND = "no_text_found"
    FAILED = "failed"  # provider ran but returned an error/malformed result
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # unreachable, no credentials, or a stub
    LIMIT_REACHED = "limit_reached"  # budget guard stopped us before OCR could run


@dataclass(frozen=True)
class OCRResult:
    status: OCRStatus
    extracted_text: str | None = None
    detected_language: str | None = None
    confidence: float | None = None
    provider_name: str | None = None
    model_version: str | None = None


class OCRProviderError(Exception):
    """Maps to OCRStatus.FAILED."""


class OCRProviderUnavailable(Exception):
    """Maps to OCRStatus.PROVIDER_UNAVAILABLE (timeout/unreachable/misconfigured)."""


class OCRProvider(Protocol):
    async def extract_text(self, image_bytes: bytes, mime_type: str, language_hint: str | None) -> OCRResult: ...


class NullOCRProvider:
    """Stub — see module docstring. Always reports PROVIDER_UNAVAILABLE,
    never a fabricated 'no text found', so the pipeline correctly routes
    this to insufficient_evidence (decisions.md §1A) instead of silently
    treating an unrun OCR pass as "the image has no text"."""

    provider_name = "null_stub_no_engine_selected"

    async def extract_text(self, image_bytes: bytes, mime_type: str, language_hint: str | None) -> OCRResult:
        log_event(
            logger,
            logging.WARNING,
            "OCR: no real, benchmarked engine configured (decisions.md §11) — reporting provider_unavailable",
        )
        return OCRResult(status=OCRStatus.PROVIDER_UNAVAILABLE, provider_name=self.provider_name)


class OCRTool:
    def __init__(self, provider: OCRProvider) -> None:
        self._provider = provider

    async def extract(self, image_bytes: bytes, mime_type: str, language_hint: str | None = None) -> OCRResult:
        # Phase 9 — provider health tracking (roadmap §9.5); see
        # app/core/provider_health.py's docstring.
        started_at = time.monotonic()
        try:
            result = await self._provider.extract_text(image_bytes, mime_type, language_hint)
            provider_health.record_success("ocr", (time.monotonic() - started_at) * 1000)
            return result
        except OCRProviderUnavailable as exc:
            provider_health.record_failure("ocr", type(exc).__name__)
            return OCRResult(status=OCRStatus.PROVIDER_UNAVAILABLE, provider_name=getattr(self._provider, "provider_name", None))
        except Exception as exc:  # noqa: BLE001 - any other provider failure
            provider_health.record_failure("ocr", type(exc).__name__)
            log_event(logger, logging.ERROR, "OCR provider raised unexpectedly")
            return OCRResult(status=OCRStatus.FAILED, provider_name=getattr(self._provider, "provider_name", None))
