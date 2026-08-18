"""
Audio validation tests (app/media/audio_validation.py) — never trusts
claimed MIME type or file extension, only magic bytes + PyAV's genuine
container/duration probe. Mirrors tests/unit/test_media_validation.py's
exact structure.
"""

from app.media.audio_validation import sniff_audio_mime_type, validate_audio
from app.media.validation import MediaValidationError
from tests.conftest import make_sample_ogg_bytes


class TestSniffAudioMimeType:
    def test_detects_ogg(self):
        assert sniff_audio_mime_type(make_sample_ogg_bytes()) == "audio/ogg"

    def test_detects_id3_tagged_mp3(self):
        assert sniff_audio_mime_type(b"ID3\x03\x00\x00\x00" + b"\x00" * 20) == "audio/mpeg"

    def test_detects_raw_mpeg_frame_sync_without_id3(self):
        assert sniff_audio_mime_type(b"\xff\xfb\x90\x00" + b"\x00" * 20) == "audio/mpeg"

    def test_detects_wav(self):
        wav_header = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE"
        assert sniff_audio_mime_type(wav_header) == "audio/wav"

    def test_detects_mp4_m4a_container(self):
        mp4_header = b"\x00\x00\x00\x18ftypM4A "
        assert sniff_audio_mime_type(mp4_header) == "audio/mp4"

    def test_detects_amr_narrowband(self):
        assert sniff_audio_mime_type(b"#!AMR\n" + b"\x00" * 10) == "audio/amr"

    def test_detects_amr_wideband(self):
        assert sniff_audio_mime_type(b"#!AMR-WB\n" + b"\x00" * 10) == "audio/amr-wb"

    def test_returns_none_for_unrecognized_bytes(self):
        assert sniff_audio_mime_type(b"not audio at all") is None


class TestValidateAudio:
    def test_accepts_valid_ogg_within_duration_limit(self):
        data = make_sample_ogg_bytes(duration_seconds=2)
        mime, duration = validate_audio(data, max_duration_seconds=60)
        assert mime == "audio/ogg"
        assert 1.5 < duration < 2.5

    def test_rejects_unsupported_format(self):
        try:
            validate_audio(b"%PDF-1.4 fake pdf content", max_duration_seconds=60)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "unsupported_format"

    def test_rejects_corrupt_audio_data(self):
        corrupt = b"OggS" + b"\x00" * 50  # valid magic bytes, garbage payload
        try:
            validate_audio(corrupt, max_duration_seconds=60)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "corrupt_audio"

    def test_rejects_audio_exceeding_duration_limit(self):
        data = make_sample_ogg_bytes(duration_seconds=5)
        try:
            validate_audio(data, max_duration_seconds=1)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "duration_too_long"

    def test_accepts_audio_within_duration_limit(self):
        data = make_sample_ogg_bytes(duration_seconds=1)
        mime, duration = validate_audio(data, max_duration_seconds=10)
        assert mime == "audio/ogg"
        assert duration < 10

    def test_never_trusts_claimed_mime_type(self):
        """validate_audio has no mime_type parameter to even accept a claim
        — enforced by the function signature itself, same as validate_image."""
        data = make_sample_ogg_bytes()
        mime, _ = validate_audio(data, max_duration_seconds=60)
        assert mime == "audio/ogg"
