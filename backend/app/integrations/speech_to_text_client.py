"""
Speech-to-Text provider client — real implementation (Sarvam AI), selected
after comparing it against Google Cloud Speech-to-Text (see
docs/risks-and-open-questions.md for the full writeup): Sarvam is ~2-3x
cheaper, purpose-built for Indian languages (22 languages incl. Malayalam/
Tamil/Hindi/English, code-mixing support relevant to real WhatsApp voice
notes that blend English words into Indic speech), traded off against being
a newer, less-proven-at-scale vendor than Google.

This is a genuine, correct client — real HTTP calls, real auth, real error
mapping — but it is NOT a benchmark result and does not by itself satisfy
decisions.md §11's "benchmarking happens before final engine selection"
rule. It stays OPT-IN (app/core/config.py: SPEECH_TO_TEXT_PROVIDER must be
set to "sarvam" AND SPEECH_TO_TEXT_API_KEY must be non-empty) — main.py's
default remains NullSpeechToTextProvider until real credentials exist and a
benchmark can actually run.

Implements the existing SpeechToTextProvider Protocol
(app/agent/tools/speech_to_text.py) — AudioPipeline/VideoPipeline never
import this class directly, only via that Protocol (through STTTool), so a
second/different real provider is a drop-in later without touching either
pipeline.

API contract (Sarvam AI /speech-to-text, verified against public docs at
implementation time — no real credentials were available to make an actual
call, so this is best-effort against documentation, not a live-tested
integration; see the module's test file for what IS verified: real request
construction and response parsing against a scripted httpx.MockTransport):
  POST https://api.sarvam.ai/speech-to-text, multipart/form-data
  Header: api-subscription-key: <API_KEY>
  Fields: file, model, language_code (BCP-47, e.g. "ml-IN"), mode=transcribe
  Response: {"transcript", "language_code", "language_probability", ...}
  Documented constraint: must complete within 30s (REST synchronous limit).

Never logs the API key, raw audio bytes, or transcript content — only
metadata (status codes, byte lengths, language codes), extending decisions.md
§7/§9's "never log sensitive content" rule from phone numbers to voice-note
content.
"""

import asyncio
import logging

import httpx

from app.agent.tools.speech_to_text import (
    STTResult,
    STTStatus,
    SpeechToTextProviderError,
    SpeechToTextProviderUnavailable,
)
from app.core.logging import log_event

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.sarvam.ai/speech-to-text"

# Internal 2-letter language codes (app/i18n/templates.py, users.preferred_language)
# <-> Sarvam's BCP-47 codes. "unknown" triggers Sarvam's own auto-detection
# when no hint is available or the hint isn't one of the four MVP languages.
_INTERNAL_TO_SARVAM = {"ml": "ml-IN", "ta": "ta-IN", "hi": "hi-IN", "en": "en-IN"}
_SARVAM_TO_INTERNAL = {v: k for k, v in _INTERNAL_TO_SARVAM.items()}

# Retried only for transient failures (timeout, 5xx) — never for 4xx
# (auth/bad-request are not retryable). Bounded per the user's explicit
# "all external calls must have... bounded retries" requirement.
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 0.5


def _normalize_detected_language(sarvam_code: str | None) -> str | None:
    if not sarvam_code:
        return None
    return _SARVAM_TO_INTERNAL.get(sarvam_code, sarvam_code)


class SarvamSpeechToTextProvider:
    """Real implementation. Never imported by tests exercising the pipeline
    — see tests/conftest.py's FakeSTTProvider for the scripted fake used
    everywhere else; this class is exercised directly, against a scripted
    httpx.MockTransport, in tests/unit/test_sarvam_stt_client.py."""

    provider_name = "sarvam"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        # Test-only injection point (httpx.MockTransport), same pattern as
        # MetaMediaClient/SecureUrlFetcher.
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"api-subscription-key": self._api_key}

    async def transcribe(self, audio_bytes: bytes, mime_type: str, language_hint: str | None) -> STTResult:
        if not self._api_key:
            # Checked before any network call — cheap, and satisfies "if
            # credentials are missing, report PROVIDER_UNAVAILABLE" without
            # a wasted round-trip.
            raise SpeechToTextProviderUnavailable("no API key configured")

        language_code = _INTERNAL_TO_SARVAM.get(language_hint or "", "unknown")
        data = {"model": self._model, "language_code": language_code, "mode": "transcribe"}

        response = await self._post_with_retries(audio_bytes, mime_type, data)
        return self._parse_response(response)

    async def _post_with_retries(self, audio_bytes: bytes, mime_type: str, data: dict[str, str]) -> httpx.Response:
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            files = {"file": ("audio", audio_bytes, mime_type)}  # rebuilt each attempt — httpx consumes the stream
            try:
                async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
                    response = await client.post(_ENDPOINT, headers=self._headers(), files=files, data=data)
            except httpx.TimeoutException as exc:
                last_exc = exc
                log_event(logger, logging.WARNING, "Sarvam STT request timed out", attempt=attempt)
            except httpx.HTTPError as exc:
                last_exc = exc
                log_event(logger, logging.WARNING, "Sarvam STT transport error", attempt=attempt)
            else:
                if response.status_code < 500:
                    return response  # 2xx/4xx are terminal — only 5xx/timeout/transport errors retry
                last_exc = SpeechToTextProviderError(f"provider server error: {response.status_code}")
                log_event(logger, logging.WARNING, "Sarvam STT server error", status_code=response.status_code, attempt=attempt)

            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

        if isinstance(last_exc, httpx.TimeoutException):
            raise SpeechToTextProviderError(f"timeout after {_MAX_RETRIES + 1} attempts: {last_exc}") from last_exc
        raise SpeechToTextProviderError(f"failed after {_MAX_RETRIES + 1} attempts: {last_exc}") from last_exc

    def _parse_response(self, response: httpx.Response) -> STTResult:
        if response.status_code in (401, 403):
            log_event(logger, logging.ERROR, "Sarvam STT authentication failed", status_code=response.status_code)
            raise SpeechToTextProviderUnavailable("authentication failed")
        if response.status_code == 429:
            log_event(logger, logging.WARNING, "Sarvam STT rate limited")
            raise SpeechToTextProviderUnavailable("rate limited")
        if response.status_code >= 400:
            log_event(logger, logging.ERROR, "Sarvam STT request rejected", status_code=response.status_code)
            raise SpeechToTextProviderError(f"request rejected: {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SpeechToTextProviderError("malformed response (not JSON)") from exc

        transcript = payload.get("transcript")
        if transcript is None or not isinstance(transcript, str):
            raise SpeechToTextProviderError("malformed response (missing/invalid transcript field)")

        if not transcript.strip():
            return STTResult(status=STTStatus.NO_SPEECH_FOUND, provider_name=self.provider_name, model_version=self._model)

        detected_language = _normalize_detected_language(payload.get("language_code"))
        confidence = payload.get("language_probability")
        if confidence is not None and not isinstance(confidence, (int, float)):
            confidence = None  # never trust a malformed field into a numeric column

        return STTResult(
            status=STTStatus.SUCCESS,
            transcript=transcript,
            detected_language=detected_language,
            confidence=confidence,
            provider_name=self.provider_name,
            model_version=self._model,
        )
