"""
Frame extraction and audio-track extraction tests (app/media/
{frame_extractor,audio_extractor}.py) — exercises the REAL PyAV decode
logic against real, PyAV-encoded fixtures (make_sample_mp4_bytes), not
canned byte strings. Covers the user's explicit resource-exhaustion
requirements: bounded frame count, bounded sampling interval, never
unlimited frames.
"""

from app.media.audio_extractor import extract_audio_track
from app.media.audio_validation import validate_audio
from app.media.frame_extractor import extract_frames
from tests.conftest import make_sample_mp4_bytes


class TestExtractFrames:
    def test_extracts_requested_number_of_frames(self):
        video = make_sample_mp4_bytes(duration_seconds=3, fps=10)
        frames = extract_frames(video, max_frames=3, sampling_interval_seconds=1.0)
        assert len(frames) == 3
        assert [f.timestamp_seconds for f in frames] == [0.0, 1.0, 2.0]

    def test_never_exceeds_max_frames_even_for_a_long_video(self):
        """The user's explicit requirement: 'never process unlimited
        frames' — a much-longer-than-needed video must still stop exactly
        at the cap."""
        video = make_sample_mp4_bytes(duration_seconds=20, fps=10)  # 200 raw frames
        frames = extract_frames(video, max_frames=5, sampling_interval_seconds=2.0)
        assert len(frames) == 5

    def test_sampling_interval_is_respected(self):
        video = make_sample_mp4_bytes(duration_seconds=6, fps=10)
        frames = extract_frames(video, max_frames=10, sampling_interval_seconds=2.0)
        timestamps = [f.timestamp_seconds for f in frames]
        for a, b in zip(timestamps, timestamps[1:]):
            assert b - a >= 2.0 - 1e-6

    def test_returns_real_jpeg_bytes(self):
        video = make_sample_mp4_bytes(duration_seconds=1, fps=10, width=32, height=32)
        frames = extract_frames(video, max_frames=1, sampling_interval_seconds=0.5)
        assert len(frames) == 1
        assert frames[0].jpeg_bytes.startswith(b"\xff\xd8\xff")  # real JPEG magic bytes

    def test_short_video_returns_fewer_frames_than_max(self):
        video = make_sample_mp4_bytes(duration_seconds=1, fps=10)
        frames = extract_frames(video, max_frames=100, sampling_interval_seconds=0.5)
        assert 0 < len(frames) < 100

    def test_corrupt_video_bytes_raise_frame_extraction_error(self):
        from app.media.frame_extractor import FrameExtractionError

        try:
            extract_frames(b"not a real video", max_frames=5, sampling_interval_seconds=1.0)
            assert False, "expected FrameExtractionError"
        except FrameExtractionError:
            pass


class TestExtractAudioTrack:
    async def test_extracts_wav_bytes_from_video_with_audio(self):
        video = make_sample_mp4_bytes(duration_seconds=2, with_audio=True)
        wav_bytes = extract_audio_track(video)
        assert wav_bytes is not None
        assert wav_bytes.startswith(b"RIFF")

        mime, duration = validate_audio(wav_bytes, max_duration_seconds=60)
        assert mime == "audio/wav"
        assert duration > 0

    async def test_returns_none_for_video_without_audio_track(self):
        video = make_sample_mp4_bytes(duration_seconds=1, with_audio=False)
        wav_bytes = extract_audio_track(video)
        assert wav_bytes is None

    async def test_corrupt_video_bytes_raise_audio_extraction_error(self):
        from app.media.audio_extractor import AudioExtractionError

        try:
            extract_audio_track(b"not a real video")
            assert False, "expected AudioExtractionError"
        except AudioExtractionError:
            pass
