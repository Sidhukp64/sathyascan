"""
ResembleAudioForensicsProvider/ResembleVideoForensicsProvider tests —
exercises the REAL request-construction and response-parsing logic (not a
fake wrapper) via httpx.MockTransport, mirroring
tests/unit/test_sarvam_stt_client.py's exact pattern.

No real credentials exist in this environment, and Resemble's exact
response schema could not be confirmed against a live call during
implementation (see the module's docstring) — these tests prove the CODE
is correct against the documented request shape and a defensively-parsed,
best-effort response shape, not that it matches Resemble's real API
byte-for-byte. Every status/label/score combination the parser recognizes
is exercised, plus its safe fallback (AUTHENTICITY_UNCERTAIN) for anything
it doesn't.
"""

import httpx
import pytest

from app.agent.tools.audio_forensics import (
    AudioForensicsStatus,
    AudioForensicsProviderError,
)
from app.agent.tools.video_forensics import (
    VideoForensicsStatus,
    VideoForensicsProviderError,
)
from app.integrations.media_forensics_client import ResembleAudioForensicsProvider, ResembleVideoForensicsProvider


def _audio_provider(handler, api_key: str = "test-key") -> ResembleAudioForensicsProvider:
    return ResembleAudioForensicsProvider(api_key=api_key, timeout_seconds=5.0, transport=httpx.MockTransport(handler))


def _video_provider(handler, api_key: str = "test-key") -> ResembleVideoForensicsProvider:
    return ResembleVideoForensicsProvider(api_key=api_key, timeout_seconds=5.0, transport=httpx.MockTransport(handler))


def _detect_response(status: str = "success", label: str | None = None, score: float | None = None) -> httpx.Response:
    body = {"status": status}
    if label is not None:
        body["label"] = label
    if score is not None:
        body["score"] = score
    return httpx.Response(200, json=body)


class TestRequestConstruction:
    async def test_sends_correct_endpoint_and_auth_header(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth_header"] = request.headers.get("authorization")
            seen["prefer_header"] = request.headers.get("prefer")
            return _detect_response(label="real", score=0.1)

        provider = _audio_provider(handler, api_key="secret-abc")
        await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert seen["url"] == "https://app.resemble.ai/api/v2/detect"
        assert seen["auth_header"] == "Bearer secret-abc"
        assert seen["prefer_header"] == "wait"

    async def test_sends_zero_retention_mode_flag(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _detect_response(label="real", score=0.1)

        provider = _audio_provider(handler)
        await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert b'name="zero_retention_mode"\r\n\r\ntrue' in seen["body"]

    async def test_sends_media_bytes_as_multipart_file(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body_contains_media"] = b"fake video bytes here" in request.content
            return _detect_response(label="real", score=0.1)

        provider = _video_provider(handler)
        await provider.analyze(b"fake video bytes here", "video/mp4")

        assert "multipart/form-data" in seen["content_type"]
        assert seen["body_contains_media"]


class TestNoApiKeyReportsUnavailableWithoutNetworkCall:
    async def test_audio_no_api_key(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _detect_response(label="real", score=0.1)

        provider = _audio_provider(handler, api_key="")
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert result.status == AudioForensicsStatus.PROVIDER_UNAVAILABLE
        assert call_count == 0

    async def test_video_no_api_key(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _detect_response(label="real", score=0.1)

        provider = _video_provider(handler, api_key="")
        result = await provider.analyze(b"fake video bytes", "video/mp4")

        assert result.status == VideoForensicsStatus.PROVIDER_UNAVAILABLE
        assert call_count == 0


class TestLabelBasedVerdictMapping:
    """The provider's own label field, when present and recognized, is
    authoritative over the score-threshold fallback."""

    @pytest.mark.parametrize(
        "label,expected_status",
        [
            ("fake", AudioForensicsStatus.AI_GENERATED_LIKELY),
            ("synthetic", AudioForensicsStatus.AI_GENERATED_LIKELY),
            ("ai_generated", AudioForensicsStatus.AI_GENERATED_LIKELY),
            ("real", AudioForensicsStatus.AI_GENERATED_UNLIKELY),
            ("authentic", AudioForensicsStatus.AI_GENERATED_UNLIKELY),
            ("manipulated", AudioForensicsStatus.MANIPULATED_LIKELY),
            ("tampered", AudioForensicsStatus.MANIPULATED_LIKELY),
        ],
    )
    async def test_known_labels_map_correctly(self, label, expected_status):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(label=label, score=0.5)

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == expected_status

    async def test_label_is_case_insensitive(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(label="FAKE", score=0.9)

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.AI_GENERATED_LIKELY


class TestScoreThresholdFallback:
    """When no recognized label is present, the provider's own confidence
    SCORE (never a client-side heuristic) is thresholded conservatively."""

    async def test_high_score_without_label_maps_to_likely_fake(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(score=0.9)  # no label field at all

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.AI_GENERATED_LIKELY
        assert result.ai_generated_probability == 0.9

    async def test_low_score_without_label_maps_to_unlikely(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(score=0.05)

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.AI_GENERATED_UNLIKELY

    async def test_ambiguous_score_maps_to_uncertain_never_a_guess(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(score=0.5)  # squarely in the ambiguous middle

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.AUTHENTICITY_UNCERTAIN

    async def test_no_label_and_no_score_maps_to_uncertain_never_a_guess(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response()  # nothing recognizable at all

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.AUTHENTICITY_UNCERTAIN
        assert result.ai_generated_probability is None


class TestProviderFailure:
    async def test_authentication_failure_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid key"})

        provider = _audio_provider(handler)
        with pytest.raises(AudioForensicsProviderError):
            await provider.analyze(b"fake audio bytes", "audio/ogg")

    async def test_rate_limit_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "rate limited"})

        provider = _audio_provider(handler)
        with pytest.raises(AudioForensicsProviderError):
            await provider.analyze(b"fake audio bytes", "audio/ogg")

    async def test_non_success_status_field_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(status="processing")  # never resolved

        provider = _audio_provider(handler)
        with pytest.raises(AudioForensicsProviderError):
            await provider.analyze(b"fake audio bytes", "audio/ogg")

    async def test_malformed_response_not_json_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        provider = _audio_provider(handler)
        with pytest.raises(AudioForensicsProviderError):
            await provider.analyze(b"fake audio bytes", "audio/ogg")

    async def test_video_authentication_failure_raises_video_specific_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        provider = _video_provider(handler)
        with pytest.raises(VideoForensicsProviderError):
            await provider.analyze(b"fake video bytes", "video/mp4")


class TestTimeoutAndRetry:
    async def test_timeout_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        provider = _audio_provider(handler)
        with pytest.raises(AudioForensicsProviderError):
            await provider.analyze(b"fake audio bytes", "audio/ogg")

    async def test_server_error_is_retried_once_and_succeeds(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(503, json={"error": "unavailable"})
            return _detect_response(label="real", score=0.05)

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert call_count == 2
        assert result.status == AudioForensicsStatus.AI_GENERATED_UNLIKELY

    async def test_persistent_server_error_raises_after_bounded_retries(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, json={"error": "internal error"})

        provider = _audio_provider(handler)
        with pytest.raises(AudioForensicsProviderError):
            await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert call_count == 2  # forensics calls are expensive — bounded to 1 retry, not 2


class TestResultCarriesProviderMetadata:
    async def test_provider_name_and_model_version_present_on_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _detect_response(label="real", score=0.1)

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert result.provider_name == "resemble"
        assert result.model_version is not None


class TestMetricsShapeResponseParsing:
    """Phase 5 final-validation follow-up: a targeted documentation search
    turned up a second, more specific plausible response shape
    (payload["metrics"]/["video_metrics"] containing label/aggregated_score/
    score/certainty) distinct from the flatter shape originally assumed.
    NEITHER shape has been confirmed against a real Resemble response (see
    the module's docstring) — the parser now tries this shape FIRST, falling
    back to the original assumption, so it's tolerant of either. These tests
    exercise the new shape specifically."""

    async def test_audio_metrics_object_with_label_and_aggregated_score(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "uuid": "abc-123",
                    "duration": 3.2,
                    "media_type": "audio",
                    "metrics": {"label": "fake", "aggregated_score": 0.91, "score": [0.88, 0.93, 0.92], "certainty": 0.8},
                },
            )

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert result.status == AudioForensicsStatus.AI_GENERATED_LIKELY
        assert result.ai_generated_probability == 0.91  # aggregated_score preferred over the score array

    async def test_video_metrics_object_uses_video_metrics_container(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "media_type": "video",
                    "video_metrics": {"label": "real", "aggregated_score": 0.05},
                },
            )

        provider = _video_provider(handler)
        result = await provider.analyze(b"fake video bytes", "video/mp4")

        assert result.status == VideoForensicsStatus.AI_GENERATED_UNLIKELY
        assert result.ai_generated_probability == 0.05

    async def test_score_as_array_without_aggregated_score_is_averaged(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "metrics": {"score": [0.6, 0.8, 0.7]}})

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert result.ai_generated_probability == pytest.approx(0.7)  # mean of [0.6, 0.8, 0.7]

    async def test_genuine_zero_score_is_never_silently_dropped(self):
        """Regression test for a real bug caught during this audit: `or`
        chaining between fallback fields treated a genuine 0.0 score
        (confidently "not AI-generated") as falsy and skipped it in favor
        of the next fallback. Fixed to use explicit None-checks."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "result": {"score": 0.0}})

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert result.ai_generated_probability == 0.0
        assert result.status == AudioForensicsStatus.AI_GENERATED_UNLIKELY

    async def test_metrics_shape_takes_priority_over_flat_shape_when_both_present(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "success", "label": "real", "score": 0.1, "metrics": {"label": "fake", "aggregated_score": 0.95}},
            )

        provider = _audio_provider(handler)
        result = await provider.analyze(b"fake audio bytes", "audio/ogg")

        assert result.status == AudioForensicsStatus.AI_GENERATED_LIKELY  # metrics shape wins
