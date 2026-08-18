"""
Speech-to-Text tool — Phase 5's Audio Analyzer transcription step, mirroring
app/agent/tools/ocr.py's exact contract shape: {audio_ref, language_hint} ->
{transcript, detected_language, confidence}, with provider_name/model_version
included per decisions.md §2's mandatory-display-fields precedent, applied
consistently to every AI-provider-backed result.

decisions.md §11 locks the same hard sequencing rule OCR follows: benchmarking
happens BEFORE final engine selection, for ASR specifically "pre-V2" — no
speech-to-text provider credentials and no curated Malayalam/Tamil/Hindi/
English voice-note benchmark set exist in this build environment, so no
engine has been benchmark-selected. NullSpeechToTextProvider is the only
provider wired; a real, benchmarked engine (Whisper/IndicWhisper/a cloud STT
API) is a drop-in replacement behind the same SpeechToTextProvider interface.

Local-language design: `language_hint` is advisory (the caller's best guess —
e.g. the user's preferred_language setting), never authoritative — a real
provider is expected to auto-detect if the hint is wrong. `detected_language`
on the result is what's actually used downstream (claim extraction, and the
final WhatsApp reply's language selection defers to the user's own language
setting, not the transcript's, per decisions.md's existing i18n pattern).
The transcript is ALWAYS kept in its original spoken language — never
translated to English before being handed to ClaimExtractionTool, matching
the same "translate for search, not for storage/display" policy already
used for evidence (decisions.md §3). Adding a fifth/sixth language later is
purely a matter of a new provider implementation; nothing in this interface
is English-centric or hard-codes a language list.

STT failure is modeled explicitly and must never silently collapse to "no
speech" — five distinct states, mirroring OCR's exact precedent:
  success | no_speech_found | failed | provider_unavailable | limit_reached
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class STTStatus(str, Enum):
    SUCCESS = "success"  # ran fine; transcript may still be "" if the audio
    # genuinely has no legible speech — see NO_SPEECH_FOUND for the explicit
    # low-confidence/silence variant a real provider may report distinctly.
    NO_SPEECH_FOUND = "no_speech_found"
    FAILED = "failed"  # provider ran but returned an error/malformed result
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # unreachable, no credentials, or a stub
    LIMIT_REACHED = "limit_reached"  # budget guard stopped us before STT could run


@dataclass(frozen=True)
class STTResult:
    status: STTStatus
    transcript: str | None = None
    detected_language: str | None = None
    confidence: float | None = None
    provider_name: str | None = None
    model_version: str | None = None


class SpeechToTextProviderError(Exception):
    """Maps to STTStatus.FAILED."""


class SpeechToTextProviderUnavailable(Exception):
    """Maps to STTStatus.PROVIDER_UNAVAILABLE (timeout/unreachable/misconfigured)."""


class SpeechToTextProvider(Protocol):
    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str | None) -> STTResult: ...


class NullSpeechToTextProvider:
    """Stub — see module docstring. Always reports PROVIDER_UNAVAILABLE,
    never a fabricated empty/silent transcript, so the pipeline correctly
    routes this to insufficient_evidence (decisions.md §1A) instead of
    silently treating an unrun transcription pass as "no speech in this
    audio"."""

    provider_name = "null_stub_no_engine_selected"

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str | None) -> STTResult:
        log_event(
            logger,
            logging.WARNING,
            "STT: no real, benchmarked engine configured (decisions.md §11) — reporting provider_unavailable",
        )
        return STTResult(status=STTStatus.PROVIDER_UNAVAILABLE, provider_name=self.provider_name)


class STTTool:
    def __init__(self, provider: SpeechToTextProvider) -> None:
        self._provider = provider

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str | None = None) -> STTResult:
        # Phase 9 — provider health tracking (roadmap §9.5), see
        # app/core/provider_health.py's docstring: this records whether the
        # provider CALL succeeded/raised, never the STTStatus it returned.
        started_at = time.monotonic()
        try:
            result = await self._provider.transcribe(audio_bytes, mime_type, language_hint)
            provider_health.record_success("speech_to_text", (time.monotonic() - started_at) * 1000)
            return result
        except SpeechToTextProviderUnavailable as exc:
            provider_health.record_failure("speech_to_text", type(exc).__name__)
            return STTResult(status=STTStatus.PROVIDER_UNAVAILABLE, provider_name=getattr(self._provider, "provider_name", None))
        except Exception as exc:  # noqa: BLE001 - any other provider failure
            provider_health.record_failure("speech_to_text", type(exc).__name__)
            log_event(logger, logging.ERROR, "STT provider raised unexpectedly")
            return STTResult(status=STTStatus.FAILED, provider_name=getattr(self._provider, "provider_name", None))
