"""
Video validation tests (app/media/video_validation.py) — never trusts
claimed MIME type or file extension, only magic bytes + PyAV's genuine
container/duration/resolution probe. Mirrors
tests/unit/test_audio_validation.py's exact structure.
"""

from app.media.validation import MediaValidationError
from app.media.video_validation import sniff_video_mime_type, validate_video
from tests.conftest import make_sample_mp4_bytes


class TestSniffVideoMimeType:
    def test_detects_mp4(self):
        assert sniff_video_mime_type(make_sample_mp4_bytes()) == "video/mp4"

    def test_detects_webm(self):
        webm_header = b"\x1a\x45\xdf\xa3" + b"\x00" * 20
        assert sniff_video_mime_type(webm_header) == "video/webm"

    def test_returns_none_for_unrecognized_bytes(self):
        assert sniff_video_mime_type(b"not video at all") is None


class TestValidateVideo:
    def test_accepts_valid_video_within_limits(self):
        data = make_sample_mp4_bytes(duration_seconds=2, width=64, height=64)
        mime, duration, width, height = validate_video(data, max_duration_seconds=60, max_pixels=10_000_000)
        assert mime == "video/mp4"
        assert 1.5 < duration < 3.0
        assert width == 64
        assert height == 64

    def test_rejects_unsupported_format(self):
        try:
            validate_video(b"%PDF-1.4 fake pdf content", max_duration_seconds=60, max_pixels=10_000_000)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "unsupported_format"

    def test_rejects_corrupt_video_data(self):
        corrupt = b"\x00\x00\x00\x18ftyp" + b"\x00" * 50  # valid magic bytes, garbage payload
        try:
            validate_video(corrupt, max_duration_seconds=60, max_pixels=10_000_000)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "corrupt_video"

    def test_rejects_video_exceeding_duration_limit(self):
        data = make_sample_mp4_bytes(duration_seconds=5)
        try:
            validate_video(data, max_duration_seconds=1, max_pixels=10_000_000)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "duration_too_long"

    def test_rejects_video_exceeding_resolution_limit(self):
        data = make_sample_mp4_bytes(duration_seconds=1, width=128, height=128)
        try:
            validate_video(data, max_duration_seconds=60, max_pixels=1000)  # 128*128=16384 > 1000
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "resolution_too_large"

    def test_accepts_video_within_resolution_limit(self):
        data = make_sample_mp4_bytes(duration_seconds=1, width=32, height=32)
        mime, _, width, height = validate_video(data, max_duration_seconds=60, max_pixels=10_000)
        assert width * height <= 10_000

    def test_video_without_audio_track_still_validates(self):
        data = make_sample_mp4_bytes(duration_seconds=1, with_audio=False)
        mime, duration, width, height = validate_video(data, max_duration_seconds=60, max_pixels=10_000_000)
        assert mime == "video/mp4"

    def test_never_trusts_claimed_mime_type(self):
        """validate_video has no mime_type parameter to even accept a claim
        — enforced by the function signature itself, same as validate_image."""
        data = make_sample_mp4_bytes()
        mime, *_ = validate_video(data, max_duration_seconds=60, max_pixels=10_000_000)
        assert mime == "video/mp4"
