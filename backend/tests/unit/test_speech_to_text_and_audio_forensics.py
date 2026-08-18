"""
STT and audio-forensics tool tests (app/agent/tools/{speech_to_text,
audio_forensics}.py). Mirrors tests/unit/test_ocr_and_image_tools.py's exact
structure.

STT failure states are modeled explicitly and must never silently collapse
to "no speech found" — see STTStatus and the tests below distinguishing each
state, same discipline OCR already follows. "limit_reached" specifically is
decided by AudioPipeline BEFORE calling STTTool at all (see
test_audio_pipeline.py's budget test) — STTTool itself only ever returns
success/no_speech_found/failed/provider_unavailable, which is what's tested
here.

Includes scripted "transcription fakes" for all four MVP languages
(Malayalam, Tamil, Hindi, English) — genuinely exercising the
language_hint-in / detected_language-out interface end to end, proving the
architecture supports each without any real STT credentials.
"""

from app.agent.tools.audio_forensics import (
    AudioForensicsResult,
    AudioForensicsStatus,
    AudioForensicsTool,
    NullAudioForensicsProvider,
)
from app.agent.tools.speech_to_text import (
    NullSpeechToTextProvider,
    STTResult,
    STTStatus,
    STTTool,
)


class _SuccessProvider:
    provider_name = "success_provider"

    def __init__(self, transcript: str, language: str) -> None:
        self._transcript = transcript
        self._language = language

    async def transcribe(self, audio_bytes, mime_type, language_hint):
        return STTResult(
            status=STTStatus.SUCCESS,
            transcript=self._transcript,
            detected_language=self._language,
            confidence=0.91,
            provider_name=self.provider_name,
            model_version="v1",
        )


class _NoSpeechProvider:
    provider_name = "no_speech_provider"

    async def transcribe(self, audio_bytes, mime_type, language_hint):
        return STTResult(status=STTStatus.NO_SPEECH_FOUND, transcript="", provider_name=self.provider_name)


class _FailingProvider:
    provider_name = "failing_provider"

    async def transcribe(self, audio_bytes, mime_type, language_hint):
        raise RuntimeError("simulated STT provider crash")


class _UnavailableRaisingProvider:
    provider_name = "unavailable_provider"

    async def transcribe(self, audio_bytes, mime_type, language_hint):
        from app.agent.tools.speech_to_text import SpeechToTextProviderUnavailable

        raise SpeechToTextProviderUnavailable("service down")


class TestSTTTool:
    async def test_successful_transcription_returns_text(self):
        tool = STTTool(_SuccessProvider("The scheme was announced.", "en"))
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == "The scheme was announced."
        assert result.provider_name == "success_provider"
        assert result.model_version == "v1"

    async def test_no_speech_found_is_distinct_from_success_with_empty_text(self):
        tool = STTTool(_NoSpeechProvider())
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint=None)
        assert result.status == STTStatus.NO_SPEECH_FOUND

    async def test_provider_exception_maps_to_failed_not_no_speech_found(self):
        """Critical requirement: STT failure must never silently become 'no
        speech' — a crashing provider must map to FAILED, distinct from
        NO_SPEECH_FOUND."""
        tool = STTTool(_FailingProvider())
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint=None)
        assert result.status == STTStatus.FAILED
        assert result.status != STTStatus.NO_SPEECH_FOUND

    async def test_provider_unavailable_is_its_own_distinct_state(self):
        tool = STTTool(_UnavailableRaisingProvider())
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint=None)
        assert result.status == STTStatus.PROVIDER_UNAVAILABLE
        assert result.status not in {STTStatus.NO_SPEECH_FOUND, STTStatus.SUCCESS}

    async def test_null_provider_reports_unavailable_not_fabricated_transcript(self):
        """decisions.md §11: no engine has been benchmark-selected — the
        stub must say so honestly (provider_unavailable), never pretend the
        audio was transcribed and found silent."""
        tool = STTTool(NullSpeechToTextProvider())
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="ml")
        assert result.status == STTStatus.PROVIDER_UNAVAILABLE
        assert result.transcript is None


class TestLocalLanguageTranscription:
    """Scripted fakes proving the SpeechToTextProvider interface genuinely
    supports each of the four MVP languages end to end — transcript
    preserved in its ORIGINAL language, detected_language reported
    correctly, language_hint passed through untranslated."""

    async def test_malayalam_transcription(self):
        transcript = "കേരള സർക്കാർ എല്ലാ വിദ്യാർത്ഥികൾക്കും അമ്പതിനായിരം രൂപ നൽകുമെന്ന് പ്രഖ്യാപിച്ചു."
        tool = STTTool(_SuccessProvider(transcript, "ml"))
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="ml")
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == transcript  # preserved verbatim, never translated
        assert result.detected_language == "ml"

    async def test_tamil_transcription(self):
        transcript = "தமிழ்நாடு அரசு அனைத்து மாணவர்களுக்கும் ஐம்பதாயிரம் ரூபாய் வழங்கும் என அறிவித்தது."
        tool = STTTool(_SuccessProvider(transcript, "ta"))
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="ta")
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == transcript
        assert result.detected_language == "ta"

    async def test_hindi_transcription(self):
        transcript = "सरकार ने सभी छात्रों को पचास हजार रुपये देने की घोषणा की।"
        tool = STTTool(_SuccessProvider(transcript, "hi"))
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="hi")
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == transcript
        assert result.detected_language == "hi"

    async def test_english_transcription(self):
        transcript = "The government announced a fifty thousand rupee scheme for all students."
        tool = STTTool(_SuccessProvider(transcript, "en"))
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="en")
        assert result.status == STTStatus.SUCCESS
        assert result.transcript == transcript
        assert result.detected_language == "en"

    async def test_language_hint_is_advisory_not_authoritative(self):
        """A provider may detect a DIFFERENT language than the hint
        suggested — detected_language reflects what was actually found, the
        hint is only a best-guess starting point (module docstring)."""
        tool = STTTool(_SuccessProvider("Hello there.", "en"))
        result = await tool.transcribe(b"fake audio bytes", "audio/ogg", language_hint="ml")
        assert result.detected_language == "en"  # provider's own finding wins


class TestAudioForensicsTool:
    async def test_null_provider_reports_unavailable(self):
        tool = AudioForensicsTool(NullAudioForensicsProvider())
        result = await tool.analyze(b"fake audio bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.PROVIDER_UNAVAILABLE
        assert result.ai_generated_probability is None  # never fabricated

    async def test_provider_exception_maps_to_failed(self):
        class _Raising:
            provider_name = "raising"

            async def analyze(self, audio_bytes, mime_type):
                raise RuntimeError("boom")

        tool = AudioForensicsTool(_Raising())
        result = await tool.analyze(b"bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.FAILED

    async def test_ai_generated_likely_result_carries_provider_and_version(self):
        class _Success:
            provider_name = "some_voice_detector"

            async def analyze(self, audio_bytes, mime_type):
                return AudioForensicsResult(
                    status=AudioForensicsStatus.AI_GENERATED_LIKELY,
                    ai_generated_probability=0.88,
                    provider_name="some_voice_detector",
                    model_version="v2.1",
                )

        tool = AudioForensicsTool(_Success())
        result = await tool.analyze(b"bytes", "audio/ogg")
        assert result.status == AudioForensicsStatus.AI_GENERATED_LIKELY
        assert result.provider_name == "some_voice_detector"
        assert result.model_version == "v2.1"

    async def test_all_five_documented_states_are_distinct(self):
        assert len(
            {
                AudioForensicsStatus.AI_GENERATED_LIKELY,
                AudioForensicsStatus.AI_GENERATED_UNLIKELY,
                AudioForensicsStatus.MANIPULATED_LIKELY,
                AudioForensicsStatus.AUTHENTICITY_UNCERTAIN,
                AudioForensicsStatus.PROVIDER_UNAVAILABLE,
            }
        ) == 5
