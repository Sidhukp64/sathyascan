"""
Provider-selection tests (app/core/provider_selection.py) — the opt-in
rule must hold exactly: a real provider is selected ONLY when both
`<X>_PROVIDER` names the known real backend AND `<X>_API_KEY` is
non-empty. Every other combination falls back to the corresponding Null
provider. This is what keeps decisions.md §11/§2 from being silently
bypassed just because credentials happen to be configured.
"""

from app.agent.tools.audio_forensics import NullAudioForensicsProvider
from app.agent.tools.speech_to_text import NullSpeechToTextProvider
from app.agent.tools.video_forensics import NullVideoForensicsProvider
from app.core.config import Settings
from app.core.provider_selection import (
    build_audio_forensics_provider,
    build_speech_to_text_provider,
    build_video_forensics_provider,
)
from app.integrations.media_forensics_client import ResembleAudioForensicsProvider, ResembleVideoForensicsProvider
from app.integrations.speech_to_text_client import SarvamSpeechToTextProvider

from tests.conftest import TEST_PHONE_ENCRYPTION_KEY, TEST_PHONE_PEPPER


def _settings(**overrides) -> Settings:
    base = dict(
        WHATSAPP_APP_SECRET="x",
        WHATSAPP_WEBHOOK_VERIFY_TOKEN="x",
        WHATSAPP_ACCESS_TOKEN="x",
        WHATSAPP_PHONE_NUMBER_ID="x",
        PHONE_HASH_PEPPER=TEST_PHONE_PEPPER,
        PHONE_ENCRYPTION_KEY=TEST_PHONE_ENCRYPTION_KEY,
    )
    base.update(overrides)
    return Settings(**base)


class TestSpeechToTextSelection:
    def test_no_provider_configured_falls_back_to_null(self):
        provider = build_speech_to_text_provider(_settings())
        assert isinstance(provider, NullSpeechToTextProvider)

    def test_provider_named_but_no_api_key_falls_back_to_null(self):
        provider = build_speech_to_text_provider(_settings(SPEECH_TO_TEXT_PROVIDER="sarvam"))
        assert isinstance(provider, NullSpeechToTextProvider)

    def test_api_key_set_but_no_provider_name_falls_back_to_null(self):
        provider = build_speech_to_text_provider(_settings(SPEECH_TO_TEXT_API_KEY="secret"))
        assert isinstance(provider, NullSpeechToTextProvider)

    def test_unknown_provider_name_falls_back_to_null(self):
        provider = build_speech_to_text_provider(
            _settings(SPEECH_TO_TEXT_PROVIDER="some_unsupported_vendor", SPEECH_TO_TEXT_API_KEY="secret")
        )
        assert isinstance(provider, NullSpeechToTextProvider)

    def test_sarvam_with_api_key_selects_real_provider(self):
        provider = build_speech_to_text_provider(
            _settings(SPEECH_TO_TEXT_PROVIDER="sarvam", SPEECH_TO_TEXT_API_KEY="secret")
        )
        assert isinstance(provider, SarvamSpeechToTextProvider)


class TestAudioForensicsSelection:
    def test_no_provider_configured_falls_back_to_null(self):
        provider = build_audio_forensics_provider(_settings())
        assert isinstance(provider, NullAudioForensicsProvider)

    def test_resemble_with_api_key_selects_real_provider(self):
        provider = build_audio_forensics_provider(
            _settings(AUDIO_FORENSICS_PROVIDER="resemble", AUDIO_FORENSICS_API_KEY="secret")
        )
        assert isinstance(provider, ResembleAudioForensicsProvider)

    def test_resemble_without_api_key_falls_back_to_null(self):
        provider = build_audio_forensics_provider(_settings(AUDIO_FORENSICS_PROVIDER="resemble"))
        assert isinstance(provider, NullAudioForensicsProvider)


class TestVideoForensicsSelection:
    def test_no_provider_configured_falls_back_to_null(self):
        provider = build_video_forensics_provider(_settings())
        assert isinstance(provider, NullVideoForensicsProvider)

    def test_resemble_with_api_key_selects_real_provider(self):
        provider = build_video_forensics_provider(
            _settings(VIDEO_FORENSICS_PROVIDER="resemble", VIDEO_FORENSICS_API_KEY="secret")
        )
        assert isinstance(provider, ResembleVideoForensicsProvider)

    def test_resemble_without_api_key_falls_back_to_null(self):
        provider = build_video_forensics_provider(_settings(VIDEO_FORENSICS_PROVIDER="resemble"))
        assert isinstance(provider, NullVideoForensicsProvider)
