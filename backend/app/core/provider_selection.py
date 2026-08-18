"""
Opt-in real-provider selection for Phase 5's STT/audio-forensics/video-
forensics tools. Centralized here (not inlined in main.py, and never
hard-coded into AudioPipeline/VideoPipeline) so the selection logic itself
is small, testable, and the pipelines stay entirely provider-agnostic —
they only ever see the SpeechToTextProvider/AudioForensicsProvider/
VideoForensicsProvider Protocol, never a concrete class.

Selection rule, identical for all three: the real provider is used ONLY
when BOTH `<X>_PROVIDER` names a known real backend AND `<X>_API_KEY` is
non-empty. Any other combination (empty provider name, unknown provider
name, empty API key) falls back to the corresponding Null provider —
never a crash, never a fabricated result. This is deliberately
conservative: decisions.md §11's "benchmark before lock-in" rule and §2's
"never self-hosted" rule aren't satisfied just because credentials exist —
selecting a real provider here only makes it AVAILABLE to opt into, it
doesn't constitute the benchmark/review those decisions still require.
"""

import logging

from app.agent.tools.audio_forensics import AudioForensicsProvider, NullAudioForensicsProvider
from app.agent.tools.ocr import NullOCRProvider, OCRProvider
from app.agent.tools.speech_to_text import NullSpeechToTextProvider, SpeechToTextProvider
from app.agent.tools.video_forensics import NullVideoForensicsProvider, VideoForensicsProvider
from app.core.config import Settings
from app.core.logging import log_event
from app.integrations.claude_client import LLMClient
from app.integrations.media_forensics_client import ResembleAudioForensicsProvider, ResembleVideoForensicsProvider
from app.integrations.speech_to_text_client import SarvamSpeechToTextProvider
from app.integrations.vision_ocr_client import ClaudeVisionOCRProvider

logger = logging.getLogger(__name__)


def build_ocr_provider(settings: Settings, llm: LLMClient) -> OCRProvider:
    """OCR follows the same opt-in rule as the three providers below, with
    one difference worth stating: its credential is `ANTHROPIC_API_KEY`, not
    a dedicated `OCR_API_KEY`, because the only real backend wired here
    (Claude vision) reuses the LLM client this deployment already needs —
    see app/integrations/vision_ocr_client.py for why that beats adding a
    second OCR engine and a second credential.

    decisions.md §11's benchmark-before-lock-in rule is NOT satisfied by
    this being selectable; it stays opt-in so the honest default is still
    'no engine selected'."""
    if settings.ocr_provider == "claude_vision" and settings.anthropic_api_key:
        log_event(logger, logging.INFO, "OCR: real provider selected", provider="claude_vision")
        return ClaudeVisionOCRProvider(llm=llm, model_version=settings.anthropic_model)
    if settings.ocr_provider:
        log_event(
            logger, logging.WARNING, "OCR: provider configured but not usable, falling back to Null",
            provider=settings.ocr_provider, has_api_key=bool(settings.anthropic_api_key),
        )
    return NullOCRProvider()


def build_speech_to_text_provider(settings: Settings) -> SpeechToTextProvider:
    if settings.speech_to_text_provider == "sarvam" and settings.speech_to_text_api_key:
        log_event(logger, logging.INFO, "speech-to-text: real provider selected", provider="sarvam")
        return SarvamSpeechToTextProvider(
            api_key=settings.speech_to_text_api_key,
            model=settings.speech_to_text_model,
            timeout_seconds=settings.speech_to_text_timeout_seconds,
        )
    if settings.speech_to_text_provider:
        log_event(
            logger, logging.WARNING, "speech-to-text: provider configured but not usable, falling back to Null",
            provider=settings.speech_to_text_provider, has_api_key=bool(settings.speech_to_text_api_key),
        )
    return NullSpeechToTextProvider()


def build_audio_forensics_provider(settings: Settings) -> AudioForensicsProvider:
    if settings.audio_forensics_provider == "resemble" and settings.audio_forensics_api_key:
        log_event(logger, logging.INFO, "audio forensics: real provider selected", provider="resemble")
        return ResembleAudioForensicsProvider(
            api_key=settings.audio_forensics_api_key,
            timeout_seconds=settings.audio_forensics_timeout_seconds,
        )
    if settings.audio_forensics_provider:
        log_event(
            logger, logging.WARNING, "audio forensics: provider configured but not usable, falling back to Null",
            provider=settings.audio_forensics_provider, has_api_key=bool(settings.audio_forensics_api_key),
        )
    return NullAudioForensicsProvider()


def build_video_forensics_provider(settings: Settings) -> VideoForensicsProvider:
    if settings.video_forensics_provider == "resemble" and settings.video_forensics_api_key:
        log_event(logger, logging.INFO, "video forensics: real provider selected", provider="resemble")
        return ResembleVideoForensicsProvider(
            api_key=settings.video_forensics_api_key,
            timeout_seconds=settings.video_forensics_timeout_seconds,
        )
    if settings.video_forensics_provider:
        log_event(
            logger, logging.WARNING, "video forensics: provider configured but not usable, falling back to Null",
            provider=settings.video_forensics_provider, has_api_key=bool(settings.video_forensics_api_key),
        )
    return NullVideoForensicsProvider()
