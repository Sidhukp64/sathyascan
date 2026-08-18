"""
LIVE Sarvam AI Speech-to-Text tests — make REAL network calls to the real
Sarvam API, no mocking, no substituted results. Excluded from the default
suite (pyproject.toml's `addopts = "-m 'not live_network'"`); run
explicitly with `pytest -m live_network`.

This file exists to close exactly the validation gap the user's Phase 5
final report flagged: "Sarvam AI STT is implemented but has never been
tested with a real API key." As of this writing, no such key has been
supplied in this environment — every test below SKIPS with an explicit,
honest reason rather than failing, and NONE of them has ever actually run
against a real Sarvam response. They exist ready to run the moment
SPEECH_TO_TEXT_API_KEY is set to a genuine credential.

Two independent gates, checked separately per test:
  1. Is SPEECH_TO_TEXT_API_KEY set at all? Without it, nothing here can run
     — every test skips immediately.
  2. Is a REAL audio sample present for this specific language at
     tests/fixtures/live_samples/<language>.ogg (or .wav/.mp3)? Without
     one, the per-language ACCURACY tests skip (they need real speech
     content to validate transcription against), but a single "round-trip"
     test still runs using a synthetic/silent clip — proving auth, request
     construction, and response parsing work against the real API, while
     being explicit that this does NOT validate transcription accuracy
     (see tests/fixtures/live_samples/README.md for the exact distinction).
"""

import os
from pathlib import Path

import pytest

from app.agent.tools.speech_to_text import STTStatus
from app.integrations.speech_to_text_client import SarvamSpeechToTextProvider

from tests.conftest import make_sample_ogg_bytes

pytestmark = pytest.mark.live_network

_SAMPLES_DIR = Path(__file__).parent.parent / "fixtures" / "live_samples"
_LANGUAGE_SAMPLE_FILES = {
    "ml": ["malayalam.ogg", "malayalam.wav", "malayalam.mp3"],
    "ta": ["tamil.ogg", "tamil.wav", "tamil.mp3"],
    "hi": ["hindi.ogg", "hindi.wav", "hindi.mp3"],
    "en": ["english.ogg", "english.wav", "english.mp3"],
}


def _require_sarvam_api_key() -> str:
    api_key = os.environ.get("SPEECH_TO_TEXT_API_KEY", "")
    if not api_key:
        pytest.skip(
            "SPEECH_TO_TEXT_API_KEY is not set — live Sarvam AI test skipped. "
            "Set it to a real Sarvam AI API key to run this test against the actual API."
        )
    return api_key


def _find_real_sample(language: str) -> Path | None:
    for filename in _LANGUAGE_SAMPLE_FILES[language]:
        candidate = _SAMPLES_DIR / filename
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _provider() -> SarvamSpeechToTextProvider:
    api_key = _require_sarvam_api_key()
    model = os.environ.get("SPEECH_TO_TEXT_MODEL", "saaras:v3")
    timeout = float(os.environ.get("SPEECH_TO_TEXT_TIMEOUT_SECONDS", "30.0"))
    return SarvamSpeechToTextProvider(api_key=api_key, model=model, timeout_seconds=timeout)


class TestLiveRoundTrip:
    """Runs whenever SPEECH_TO_TEXT_API_KEY is set, regardless of whether
    real sample media exists — proves the real API responds, auth works,
    and the response parses, using a synthetic (silent) clip if nothing
    real is available. This does NOT validate transcription accuracy."""

    async def test_real_api_call_succeeds_and_response_parses(self):
        provider = _provider()
        sample = _find_real_sample("en")
        audio_bytes = sample.read_bytes() if sample else make_sample_ogg_bytes(duration_seconds=2)

        result = await provider.transcribe(audio_bytes, "audio/ogg", language_hint="en")

        # A real API call was made and a real response was parsed into our
        # typed result — this is the actual claim this test can honestly
        # make. It does NOT assert the transcript is linguistically correct.
        assert result.status in (STTStatus.SUCCESS, STTStatus.NO_SPEECH_FOUND)
        assert result.provider_name == "sarvam"
        print(f"\n[LIVE] Sarvam round-trip: status={result.status}, "
              f"used_real_sample={sample is not None}, "
              f"detected_language={result.detected_language}")


class TestLiveLanguageAccuracy:
    """Each of these ONLY runs its real-transcription assertion when a real
    sample file exists for that language — otherwise it skips with an
    explicit reason. Never falls back to asserting anything about a
    synthetic clip's 'accuracy' (a silent clip has no correct transcript
    to check against)."""

    @pytest.mark.parametrize("language,language_name", [("ml", "Malayalam"), ("ta", "Tamil"), ("hi", "Hindi"), ("en", "English")])
    async def test_real_sample_transcription(self, language, language_name):
        provider = _provider()  # still checks the API key first, even if the sample is also missing
        sample = _find_real_sample(language)
        if sample is None:
            pytest.skip(
                f"No real {language_name} audio sample found at "
                f"tests/fixtures/live_samples/{_LANGUAGE_SAMPLE_FILES[language][0]} (or .wav/.mp3) — "
                f"live {language_name} transcription accuracy cannot be validated without one. "
                f"See tests/fixtures/live_samples/README.md."
            )

        audio_bytes = sample.read_bytes()
        mime_type = {"ogg": "audio/ogg", "wav": "audio/wav", "mp3": "audio/mpeg"}[sample.suffix.lstrip(".")]

        result = await provider.transcribe(audio_bytes, mime_type, language_hint=language)

        print(f"\n[LIVE] {language_name} transcript: status={result.status}, "
              f"detected_language={result.detected_language}, "
              f"transcript={result.transcript!r}, confidence={result.confidence}")

        assert result.status == STTStatus.SUCCESS, (
            f"Expected a successful transcription of the real {language_name} sample, got {result.status}"
        )
        assert result.transcript and result.transcript.strip(), "Transcript must not be empty for real speech audio"
        # Deliberately NOT asserting the transcript's exact text or
        # detected_language value — a human must review the printed
        # transcript above to judge real-world accuracy; this test proves
        # the pipeline produced SOMETHING from real speech, not that it's
        # word-perfect.
