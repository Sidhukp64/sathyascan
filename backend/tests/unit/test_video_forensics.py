"""
Video forensics tool tests (app/agent/tools/video_forensics.py). Mirrors
tests/unit/test_speech_to_text_and_audio_forensics.py's TestAudioForensicsTool
structure exactly — same status vocabulary, same never-fabricate discipline.
"""

from app.agent.tools.video_forensics import (
    NullVideoForensicsProvider,
    VideoForensicsResult,
    VideoForensicsStatus,
    VideoForensicsTool,
)


class TestVideoForensicsTool:
    async def test_null_provider_reports_unavailable(self):
        tool = VideoForensicsTool(NullVideoForensicsProvider())
        result = await tool.analyze(b"fake video bytes", "video/mp4")
        assert result.status == VideoForensicsStatus.PROVIDER_UNAVAILABLE
        assert result.ai_generated_probability is None  # never fabricated

    async def test_provider_exception_maps_to_failed(self):
        class _Raising:
            provider_name = "raising"

            async def analyze(self, video_bytes, mime_type):
                raise RuntimeError("boom")

        tool = VideoForensicsTool(_Raising())
        result = await tool.analyze(b"bytes", "video/mp4")
        assert result.status == VideoForensicsStatus.FAILED

    async def test_ai_generated_likely_result_carries_provider_and_version(self):
        class _Success:
            provider_name = "some_deepfake_detector"

            async def analyze(self, video_bytes, mime_type):
                return VideoForensicsResult(
                    status=VideoForensicsStatus.AI_GENERATED_LIKELY,
                    ai_generated_probability=0.93,
                    provider_name="some_deepfake_detector",
                    model_version="v3",
                )

        tool = VideoForensicsTool(_Success())
        result = await tool.analyze(b"bytes", "video/mp4")
        assert result.status == VideoForensicsStatus.AI_GENERATED_LIKELY
        assert result.provider_name == "some_deepfake_detector"
        assert result.model_version == "v3"

    async def test_all_five_documented_states_are_distinct(self):
        assert len(
            {
                VideoForensicsStatus.AI_GENERATED_LIKELY,
                VideoForensicsStatus.AI_GENERATED_UNLIKELY,
                VideoForensicsStatus.MANIPULATED_LIKELY,
                VideoForensicsStatus.AUTHENTICITY_UNCERTAIN,
                VideoForensicsStatus.PROVIDER_UNAVAILABLE,
            }
        ) == 5
