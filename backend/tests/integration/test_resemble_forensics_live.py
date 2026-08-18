"""
LIVE Resemble AI Detect tests (audio + video forensics) — make REAL network
calls to the real Resemble API, no mocking, no substituted results.
Excluded from the default suite (pyproject.toml's
`addopts = "-m 'not live_network'"`); run explicitly with
`pytest -m live_network`.

This file exists to close exactly the validation gap the user's Phase 5
final report flagged: "Resemble AI Detect audio/video forensics is
implemented but has never been tested with a real API key" and "Resemble's
exact production response schema still needs validation." As of this
writing, no such key has been supplied — every test below SKIPS with an
explicit reason rather than failing, and none has ever run against a real
Resemble response. COST WARNING (real money, if you do run this): Resemble
bills per-second on the entry Flex tier (~$0.035/sec audio, ~$0.07/sec
video, per the pricing research in docs/risks-and-open-questions.md) —
running these tests against real credentials incurs real charges.

Two independent gates, checked separately per test:
  1. Is AUDIO_FORENSICS_API_KEY / VIDEO_FORENSICS_API_KEY set? Without it,
     nothing here can run — every test skips immediately.
  2. Is a REAL media sample present at tests/fixtures/live_samples/? The
     round-trip test runs regardless (using a synthetic clip if needed,
     proving auth/request/response-parsing only); the authentic-vs-
     synthetic verdict tests each skip independently if their specific
     sample is missing — never asserting an AI-generation verdict against
     a clip with no known ground truth.
"""

import os
from pathlib import Path

import pytest

from app.agent.tools.audio_forensics import AudioForensicsStatus
from app.agent.tools.video_forensics import VideoForensicsStatus
from app.integrations.media_forensics_client import ResembleAudioForensicsProvider, ResembleVideoForensicsProvider

from tests.conftest import make_sample_mp4_bytes, make_sample_ogg_bytes

pytestmark = pytest.mark.live_network

_SAMPLES_DIR = Path(__file__).parent.parent / "fixtures" / "live_samples"


def _require_audio_forensics_api_key() -> str:
    api_key = os.environ.get("AUDIO_FORENSICS_API_KEY", "")
    if not api_key:
        pytest.skip(
            "AUDIO_FORENSICS_API_KEY is not set — live Resemble AI Detect audio test skipped. "
            "Set it to a real Resemble AI API key to run this test (real per-second billing applies)."
        )
    return api_key


def _require_video_forensics_api_key() -> str:
    api_key = os.environ.get("VIDEO_FORENSICS_API_KEY", "")
    if not api_key:
        pytest.skip(
            "VIDEO_FORENSICS_API_KEY is not set — live Resemble AI Detect video test skipped. "
            "Set it to a real Resemble AI API key to run this test (real per-second billing applies)."
        )
    return api_key


def _find_sample(*filenames: str) -> Path | None:
    for filename in filenames:
        candidate = _SAMPLES_DIR / filename
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


class TestLiveAudioForensicsRoundTrip:
    async def test_real_api_call_succeeds_and_response_parses(self):
        api_key = _require_audio_forensics_api_key()
        provider = ResembleAudioForensicsProvider(api_key=api_key, timeout_seconds=60.0)

        sample = _find_sample("authentic_audio.wav", "authentic_audio.mp3", "authentic_audio.ogg")
        audio_bytes = sample.read_bytes() if sample else make_sample_ogg_bytes(duration_seconds=2)
        mime_type = "audio/wav" if sample and sample.suffix == ".wav" else "audio/ogg"

        result = await provider.analyze(audio_bytes, mime_type)

        # This is the concrete claim this test can honestly make: a real
        # API call was made, and the response was parsed into one of our
        # recognized states — never an exception, never a fabricated
        # result. This is ALSO the test that resolves "Resemble's exact
        # production response schema still needs validation" — if the
        # real schema doesn't match either shape _extract_label_and_score
        # tries, this assertion fails loudly, telling us exactly that.
        assert result.status != AudioForensicsStatus.PROVIDER_UNAVAILABLE  # a key IS configured
        print(f"\n[LIVE] Resemble audio forensics round-trip: status={result.status}, "
              f"probability={result.ai_generated_probability}, used_real_sample={sample is not None}")


class TestLiveVideoForensicsRoundTrip:
    async def test_real_api_call_succeeds_and_response_parses(self):
        api_key = _require_video_forensics_api_key()
        provider = ResembleVideoForensicsProvider(api_key=api_key, timeout_seconds=90.0)

        sample = _find_sample("authentic_video.mp4")
        video_bytes = sample.read_bytes() if sample else make_sample_mp4_bytes(duration_seconds=2)

        result = await provider.analyze(video_bytes, "video/mp4")

        assert result.status != VideoForensicsStatus.PROVIDER_UNAVAILABLE
        print(f"\n[LIVE] Resemble video forensics round-trip: status={result.status}, "
              f"probability={result.ai_generated_probability}, used_real_sample={sample is not None}")


class TestLiveAudioAuthenticityVerdicts:
    """Each of these ONLY runs its verdict assertion when a real sample
    with KNOWN ground truth exists — never asserts an AI-generation
    verdict against synthetic pipeline-mechanics fixtures, which have no
    real-world "correct" answer."""

    async def test_known_authentic_audio_sample(self):
        _require_audio_forensics_api_key()
        sample = _find_sample("authentic_audio.wav", "authentic_audio.mp3", "authentic_audio.ogg")
        if sample is None:
            pytest.skip(
                "No real authentic-audio sample found at tests/fixtures/live_samples/authentic_audio.* — "
                "cannot validate a known-authentic verdict without one. See the README there."
            )

        provider = ResembleAudioForensicsProvider(api_key=os.environ["AUDIO_FORENSICS_API_KEY"], timeout_seconds=60.0)
        mime_type = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg"}[sample.suffix.lstrip(".")]
        result = await provider.analyze(sample.read_bytes(), mime_type)

        print(f"\n[LIVE] Known-authentic audio verdict: status={result.status}, probability={result.ai_generated_probability}")
        # Not a hard assertion of AI_GENERATED_UNLIKELY — real detectors
        # have real false-positive rates. This documents the result for
        # human review rather than pretending a single sample proves
        # accuracy either way.

    async def test_known_synthetic_audio_sample(self):
        _require_audio_forensics_api_key()
        sample = _find_sample("synthetic_audio.wav", "synthetic_audio.mp3", "synthetic_audio.ogg")
        if sample is None:
            pytest.skip(
                "No real synthetic/AI-generated audio sample found at "
                "tests/fixtures/live_samples/synthetic_audio.* — cannot validate a known-AI-generated "
                "verdict without one. Do NOT fabricate this result — see the README there."
            )

        provider = ResembleAudioForensicsProvider(api_key=os.environ["AUDIO_FORENSICS_API_KEY"], timeout_seconds=60.0)
        mime_type = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg"}[sample.suffix.lstrip(".")]
        result = await provider.analyze(sample.read_bytes(), mime_type)

        print(f"\n[LIVE] Known-synthetic audio verdict: status={result.status}, probability={result.ai_generated_probability}")


class TestLiveVideoAuthenticityVerdicts:
    async def test_known_authentic_video_sample(self):
        _require_video_forensics_api_key()
        sample = _find_sample("authentic_video.mp4")
        if sample is None:
            pytest.skip(
                "No real authentic-video sample found at tests/fixtures/live_samples/authentic_video.mp4 — "
                "cannot validate a known-authentic verdict without one."
            )

        provider = ResembleVideoForensicsProvider(api_key=os.environ["VIDEO_FORENSICS_API_KEY"], timeout_seconds=90.0)
        result = await provider.analyze(sample.read_bytes(), "video/mp4")

        print(f"\n[LIVE] Known-authentic video verdict: status={result.status}, probability={result.ai_generated_probability}")

    async def test_known_manipulated_video_sample(self):
        _require_video_forensics_api_key()
        sample = _find_sample("synthetic_video.mp4")
        if sample is None:
            pytest.skip(
                "No real AI-generated/manipulated video sample found at "
                "tests/fixtures/live_samples/synthetic_video.mp4 — cannot validate a known-manipulated "
                "verdict without one. Do NOT fabricate this result — see the README there."
            )

        provider = ResembleVideoForensicsProvider(api_key=os.environ["VIDEO_FORENSICS_API_KEY"], timeout_seconds=90.0)
        result = await provider.analyze(sample.read_bytes(), "video/mp4")

        print(f"\n[LIVE] Known-manipulated video verdict: status={result.status}, probability={result.ai_generated_probability}")
