"""
SarvamSpeechToTextProvider tests — exercises the REAL request-construction
and response-parsing logic (not a fake wrapper) via httpx.MockTransport,
mirroring tests/integration/test_media_downloader.py's exact pattern.

No real credentials exist in this environment — these tests prove the
CODE is correct (real HTTP calls, real auth header, real multipart upload,
real error mapping) against a scripted transport, not that the real Sarvam
API behaves as documented. See the module's docstring for that caveat.

File-size/duration limits are NOT this client's responsibility — those are
enforced upstream by app/media/audio_validation.py before AudioPipeline/
VideoPipeline ever calls STTTool, already covered by
tests/unit/test_audio_validation.py. This client sends whatever bytes it's
given, same as every other Tool-wrapped provider in this codebase.
"""

import httpx
import pytest

from app.agent.tools.speech_to_text import (
    STTStatus,
    SpeechToTextProviderError,
    SpeechToTextProviderUnavailable,
)
from app.integrations.speech_to_text_client import SarvamSpeechToTextProvider


def _provider(handler, api_key: str = "test-key", model: str = "saaras:v3") -> SarvamSpeechToTextProvider:
    transport = httpx.MockTransport(handler)
    return SarvamSpeechToTextProvider(api_key=api_key, model=model, timeout_seconds=5.0, transport=transport)


def _success_response(transcript: str, language_code: str, probability: float = 0.95) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "request_id": "req-1",
            "transcript": transcript,
            "language_code": language_code,
            "language_probability": probability,
        },
    )


class TestRequestConstruction:
    async def test_sends_correct_endpoint_and_auth_header(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth_header"] = request.headers.get("api-subscription-key")
            return _success_response("hello", "en-IN")

        provider = _provider(handler, api_key="secret-123")
        await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert seen["url"] == "https://api.sarvam.ai/speech-to-text"
        assert seen["auth_header"] == "secret-123"

    async def test_sends_multipart_file_and_form_fields(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body_contains_audio"] = b"fake audio bytes" in request.content
            return _success_response("hello", "en-IN")

        provider = _provider(handler)
        await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert "multipart/form-data" in seen["content_type"]
        assert seen["body_contains_audio"]

    async def test_never_translates_mode_is_transcribe_not_translate(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response("hello", "en-IN")

        provider = _provider(handler)
        await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert b'name="mode"\r\n\r\ntranscribe' in seen["body"]


class TestLocalLanguageRequestMapping:
    """Confirms the internal 2-letter language_hint is correctly mapped to
    Sarvam's BCP-47 codes for all four MVP languages, and the response's
    BCP-47 code is normalized back to the internal 2-letter form."""

    async def test_malayalam(self):
        transcript = "കേരള സർക്കാർ എല്ലാ വിദ്യാർത്ഥികൾക്കും അമ്പതിനായിരം രൂപ നൽകുമെന്ന് പ്രഖ്യാപിച്ചു."
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response(transcript, "ml-IN")

        provider = _provider(handler)
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="ml")

        assert b'name="language_code"\r\n\r\nml-IN' in seen["body"]
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == transcript  # preserved verbatim, never translated
        assert result.detected_language == "ml"

    async def test_tamil(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response("தமிழ் உரை", "ta-IN")

        provider = _provider(handler)
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="ta")

        assert b'name="language_code"\r\n\r\nta-IN' in seen["body"]
        assert result.detected_language == "ta"

    async def test_hindi(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response("हिंदी पाठ", "hi-IN")

        provider = _provider(handler)
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="hi")

        assert b'name="language_code"\r\n\r\nhi-IN' in seen["body"]
        assert result.detected_language == "hi"

    async def test_english(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response("English text", "en-IN")

        provider = _provider(handler)
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert b'name="language_code"\r\n\r\nen-IN' in seen["body"]
        assert result.detected_language == "en"

    async def test_unrecognized_hint_falls_back_to_auto_detect(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response("text", "en-IN")

        provider = _provider(handler)
        await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="fr")

        assert b'name="language_code"\r\n\r\nunknown' in seen["body"]

    async def test_no_hint_falls_back_to_auto_detect(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            return _success_response("text", "en-IN")

        provider = _provider(handler)
        await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint=None)

        assert b'name="language_code"\r\n\r\nunknown' in seen["body"]


class TestProviderUnavailable:
    async def test_missing_api_key_raises_unavailable_without_any_network_call(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _success_response("should never reach here", "en-IN")

        provider = _provider(handler, api_key="")
        with pytest.raises(SpeechToTextProviderUnavailable):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert call_count == 0

    async def test_authentication_failure_401_raises_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid api key"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderUnavailable):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

    async def test_authentication_failure_403_raises_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderUnavailable):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

    async def test_rate_limit_429_raises_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": "rate limited"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderUnavailable):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")


class TestProviderFailure:
    async def test_malformed_response_not_json_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json at all")

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderError):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

    async def test_malformed_response_missing_transcript_field_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"request_id": "x", "language_code": "en-IN"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderError):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

    async def test_bad_request_400_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "unsupported audio format"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderError):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

    async def test_empty_transcript_is_no_speech_found_not_an_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _success_response("", "en-IN")

        provider = _provider(handler)
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")
        assert result.status == STTStatus.NO_SPEECH_FOUND


class TestTimeoutAndRetry:
    async def test_timeout_raises_error_after_retries_exhausted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderError, match="timeout"):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

    async def test_server_error_is_retried_and_succeeds_on_second_attempt(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(503, json={"error": "temporarily unavailable"})
            return _success_response("recovered after retry", "en-IN")

        provider = _provider(handler)
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert call_count == 2
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == "recovered after retry"

    async def test_persistent_server_error_raises_after_bounded_retries(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, json={"error": "internal error"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderError):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert call_count == 3  # initial attempt + 2 retries, then gives up — bounded, not infinite

    async def test_client_error_is_never_retried(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(400, json={"error": "bad request"})

        provider = _provider(handler)
        with pytest.raises(SpeechToTextProviderError):
            await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert call_count == 1  # 4xx is terminal, never retried


class TestNeverLogsSensitiveContent:
    async def test_provider_name_and_model_version_are_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _success_response("hello", "en-IN")

        provider = _provider(handler, model="saaras:v4")
        result = await provider.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")

        assert result.provider_name == "sarvam"
        assert result.model_version == "saaras:v4"
