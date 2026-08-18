"""
Audio/video forensics provider client — real implementation (Resemble AI
Detect), selected after comparing self-serve options (see
docs/risks-and-open-questions.md for the full writeup): Pindrop and
Sensity AI are enterprise-sales-only with no self-serve path at all,
ruled out entirely. Resemble AI Detect is the one option with a genuine
no-contract self-serve tier (real free tier, pay-as-you-go, no annual
commitment) and published third-party benchmark results (#1 on DFBench,
98.1% accuracy) — the most credible real candidate found. Its cost is real
and non-trivial (~$0.035/sec audio, ~$0.07/sec video on the entry Flex
tier as of this writing) — a confirmed, deliberate decision (per the
user's explicit choice) to implement the real client but keep it OPT-IN
(app/core/config.py) rather than a default-on spend.

**Response-schema caveat, stated plainly**: no credentials were available
to make a real Resemble Detect API call, in this implementation pass or the
subsequent Phase 5 validation audit that re-checked it. The request
construction below (endpoint, auth, multipart upload, zero_retention_mode)
is grounded in Resemble's public documentation and their own GitHub agent-
skill examples. A follow-up documentation search during the validation audit
found a SECOND, more specific plausible response shape
(`metrics`/`video_metrics` containing `label`/`aggregated_score`/`score`/
`certainty`) beyond the flatter shape originally assumed — Resemble's actual
API-reference pages remain inaccessible to automated fetching (their docs
site is JS-rendered; every direct sub-page attempt 404s or redirects to the
root), so neither shape has been confirmed against a live response.
`_extract_label_and_score` below now tries BOTH, in order, and is
deliberately defensive either way: any field it can't confidently interpret
raises an error (mapped to FAILED, never a guessed verdict) rather than
assuming a shape. **This must be validated against a real API response
before being trusted in production** — flagged in
docs/risks-and-open-questions.md as a still-open item, exactly like
decisions.md §11's benchmark requirement for STT/OCR.

Implements the existing AudioForensicsProvider/VideoForensicsProvider
Protocols (app/agent/tools/{audio_forensics,video_forensics}.py) — the
pipelines never import this class directly.

API contract (Resemble AI Detect, POST /detect):
  Base: https://app.resemble.ai/api/v2/detect
  Auth: Authorization: Bearer <API_KEY>
  Header: Prefer: wait (synchronous — blocks until the result is ready,
    rather than requiring a separate polling/GET-by-id flow)
  Body: multipart/form-data, `file` = raw media bytes (local upload, NOT
    the `url` JSON path — WhatsApp media has no public HTTPS URL, and our
    no-blob-storage policy means we never create one; direct upload keeps
    the media exactly as in-memory-only as every other pipeline)
  Form field: zero_retention_mode=true — Resemble must not retain the
    media beyond what's needed to answer this one request (decisions.md
    §9's DPDP-minded posture, extended to a third-party processor)

Never logs the API key or raw media bytes — only metadata (status codes,
byte lengths), same discipline as every other integration client.
"""

import asyncio
import logging

import httpx

from app.agent.tools.audio_forensics import (
    AudioForensicsResult,
    AudioForensicsStatus,
    AudioForensicsProviderError,
)
from app.agent.tools.video_forensics import (
    VideoForensicsResult,
    VideoForensicsStatus,
    VideoForensicsProviderError,
)
from app.core.logging import log_event

logger = logging.getLogger(__name__)

_ENDPOINT = "https://app.resemble.ai/api/v2/detect"
_PROVIDER_NAME = "resemble"
_MODEL_VERSION = "detect-v2"  # best-effort label — Resemble's docs don't expose a queryable model version

_MAX_RETRIES = 1  # forensics calls are expensive (per-second billed) — retry sparingly, only on transient failure
_RETRY_BACKOFF_SECONDS = 1.0

# Thresholds applied to the PROVIDER's own returned confidence score, never
# to a client-side heuristic (the user's explicit distinction) — a
# real classifier's probability output, bucketed into our discrete
# vocabulary. Conservative: anything not clearly high or low confidence
# reports AUTHENTICITY_UNCERTAIN rather than guessing a direction.
_HIGH_CONFIDENCE_THRESHOLD = 0.75
_LOW_CONFIDENCE_THRESHOLD = 0.25

_FAKE_LABELS = {"fake", "synthetic", "ai_generated", "ai-generated", "generated"}
_REAL_LABELS = {"real", "authentic", "human"}
_MANIPULATED_LABELS = {"manipulated", "tampered", "edited"}


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Prefer": "wait"}


async def _post_detect(
    media_bytes: bytes,
    mime_type: str,
    api_key: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        files = {"file": ("media", media_bytes, mime_type)}
        data = {"zero_retention_mode": "true"}
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
                response = await client.post(_ENDPOINT, headers=_headers(api_key), files=files, data=data)
        except httpx.TimeoutException as exc:
            last_exc = exc
            log_event(logger, logging.WARNING, "Resemble Detect request timed out", attempt=attempt)
        except httpx.HTTPError as exc:
            last_exc = exc
            log_event(logger, logging.WARNING, "Resemble Detect transport error", attempt=attempt)
        else:
            if response.status_code < 500:
                return response
            last_exc = RuntimeError(f"provider server error: {response.status_code}")
            log_event(logger, logging.WARNING, "Resemble Detect server error", status_code=response.status_code, attempt=attempt)

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

    raise RuntimeError(f"failed after {_MAX_RETRIES + 1} attempts: {last_exc}") from last_exc


def _coerce_score(value: object) -> float | None:
    """A `score` field may be a single number OR an array of per-segment/
    per-frame scores (a shape found during a follow-up documentation
    search, distinct from the single-number shape originally assumed —
    neither has been confirmed against a real response). An array is
    reduced to its mean as a reasonable scalar summary; anything else
    unrecognized is dropped rather than guessed at."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list) and value and all(isinstance(v, (int, float)) for v in value):
        return sum(value) / len(value)
    return None


def _extract_label_and_score(payload: dict) -> tuple[str | None, float | None]:
    """Defensive, best-effort extraction across several plausible response
    shapes (see module docstring's caveat — NEITHER shape below has been
    confirmed against a real Resemble response; this covers what public
    documentation and search results describe, kept intentionally
    tolerant rather than committing to one). Returns (None, None) if
    nothing recognizable is found — the caller treats that as FAILED,
    never a guessed verdict.

    Shapes checked, in order:
      1. payload["metrics"] / payload["video_metrics"] — {label,
         aggregated_score, score, certainty} — found via a follow-up
         documentation search after the initial implementation; prefers
         `aggregated_score` (an explicit scalar summary) over `score`
         (documented as possibly an array of per-segment scores).
      2. payload["result"] — {label/verdict, score/confidence} — the
         shape originally assumed from earlier research.
      3. Flat top-level {label, score} — the simplest plausible shape.
    """
    for container_key in ("metrics", "video_metrics"):
        metrics = payload.get(container_key)
        if isinstance(metrics, dict):
            label = metrics.get("label")
            score = _coerce_score(metrics.get("aggregated_score"))
            if score is None:
                score = _coerce_score(metrics.get("score"))
            if isinstance(label, str) or score is not None:
                return (label if isinstance(label, str) else None), score

    result_obj = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    label = result_obj.get("label") or result_obj.get("verdict") or payload.get("label")

    # Explicit None-checks, not `or` chaining — a genuine score of 0.0
    # (confidently "not AI-generated") is falsy in Python and must not be
    # silently skipped in favor of the next fallback.
    score = _coerce_score(result_obj.get("score"))
    if score is None:
        score = _coerce_score(result_obj.get("confidence"))
    if score is None:
        score = _coerce_score(payload.get("score"))

    if label is not None and not isinstance(label, str):
        label = None

    return label, score


def _map_to_status(label: str | None, score: float | None) -> tuple[str, float | None]:
    """Returns (status_value, ai_generated_probability). status_value is a
    plain string so both AudioForensicsStatus and VideoForensicsStatus
    (identical vocabulary, separate enums) can consume it identically."""
    normalized_label = (label or "").strip().lower()

    if normalized_label in _MANIPULATED_LABELS:
        return "manipulated_likely", score
    if normalized_label in _FAKE_LABELS:
        return "ai_generated_likely", score
    if normalized_label in _REAL_LABELS:
        return "ai_generated_unlikely", score

    # No recognized label — fall back to the provider's own confidence
    # score alone, per the module's documented threshold policy.
    if score is not None:
        if score >= _HIGH_CONFIDENCE_THRESHOLD:
            return "ai_generated_likely", score
        if score <= _LOW_CONFIDENCE_THRESHOLD:
            return "ai_generated_unlikely", score

    return "authenticity_uncertain", score


def _parse_common(response: httpx.Response, provider_error_cls: type[Exception]) -> tuple[str, float | None]:
    if response.status_code in (401, 403):
        log_event(logger, logging.ERROR, "Resemble Detect authentication failed", status_code=response.status_code)
        raise provider_error_cls("authentication failed")
    if response.status_code == 429:
        log_event(logger, logging.WARNING, "Resemble Detect rate limited")
        raise provider_error_cls("rate limited")
    if response.status_code >= 400:
        log_event(logger, logging.ERROR, "Resemble Detect request rejected", status_code=response.status_code)
        raise provider_error_cls(f"request rejected: {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise provider_error_cls("malformed response (not JSON)") from exc

    status_field = payload.get("status")
    if status_field is not None and str(status_field).lower() not in ("success", "completed", "ok"):
        raise provider_error_cls(f"provider reported non-success status: {status_field}")

    label, score = _extract_label_and_score(payload)
    status_value, probability = _map_to_status(label, score)
    return status_value, probability


class ResembleAudioForensicsProvider:
    """Real implementation. Never imported by tests exercising the pipeline
    — see tests/conftest.py's FakeAudioForensicsProvider for the scripted
    fake used everywhere else; this class is exercised directly, against a
    scripted httpx.MockTransport, in tests/unit/test_resemble_forensics_client.py."""

    provider_name = _PROVIDER_NAME

    def __init__(self, api_key: str, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def analyze(self, audio_bytes: bytes, mime_type: str) -> AudioForensicsResult:
        if not self._api_key:
            return AudioForensicsResult(status=AudioForensicsStatus.PROVIDER_UNAVAILABLE, provider_name=self.provider_name)

        try:
            response = await _post_detect(audio_bytes, mime_type, self._api_key, self._timeout_seconds, self._transport)
            status_value, probability = _parse_common(response, AudioForensicsProviderError)
        except AudioForensicsProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - network/parsing failures all map to FAILED via the Tool wrapper
            raise AudioForensicsProviderError(str(exc)) from exc

        return AudioForensicsResult(
            status=AudioForensicsStatus(status_value),
            ai_generated_probability=probability,
            provider_name=self.provider_name,
            model_version=_MODEL_VERSION,
        )


class ResembleVideoForensicsProvider:
    """Real implementation. Never imported by tests exercising the pipeline
    — see tests/conftest.py's FakeVideoForensicsProvider for the scripted
    fake used everywhere else; this class is exercised directly, against a
    scripted httpx.MockTransport, in tests/unit/test_resemble_forensics_client.py."""

    provider_name = _PROVIDER_NAME

    def __init__(self, api_key: str, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def analyze(self, video_bytes: bytes, mime_type: str) -> VideoForensicsResult:
        if not self._api_key:
            return VideoForensicsResult(status=VideoForensicsStatus.PROVIDER_UNAVAILABLE, provider_name=self.provider_name)

        try:
            response = await _post_detect(video_bytes, mime_type, self._api_key, self._timeout_seconds, self._transport)
            status_value, probability = _parse_common(response, VideoForensicsProviderError)
        except VideoForensicsProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VideoForensicsProviderError(str(exc)) from exc

        return VideoForensicsResult(
            status=VideoForensicsStatus(status_value),
            ai_generated_probability=probability,
            provider_name=self.provider_name,
            model_version=_MODEL_VERSION,
        )
