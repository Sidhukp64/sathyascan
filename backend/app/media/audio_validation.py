"""
Audio content validation. Never trusts a caller-supplied MIME type or file
extension — the actual container format is sniffed from magic bytes, and
PyAV (`av`) is used only to probe genuine duration and structural validity,
never to trust metadata the file itself claims about itself. Mirrors
app/media/validation.py's image-validation approach exactly, and reuses its
MediaValidationError rather than defining a parallel one.

Duration is checked ONLY via the container's own decoded/parsed metadata
(never a caller-supplied "duration" field, which doesn't exist in WhatsApp's
media metadata anyway) — this is the audio-specific half of the abuse-
protection ceiling decisions.md §12 requires (max file size is enforced
earlier, at download time, by the same MetaMediaClient/max_size_bytes
mechanism Phase 3 already uses for images).
"""

import io

import av

from app.media.validation import MediaValidationError

# Magic-byte signatures for the containers WhatsApp actually sends: voice
# notes are audio/ogg (Opus); "audio files" forwarded as documents are
# commonly MP3, MP4/M4A (AAC), AMR, or WAV.
_OGG_SIGNATURE = b"OggS"
_ID3_SIGNATURE = b"ID3"
_AMR_NB_SIGNATURE = b"#!AMR\n"
_AMR_WB_SIGNATURE = b"#!AMR-WB\n"

SUPPORTED_AUDIO_MIME_TYPES = frozenset(
    {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr", "audio/amr-wb", "audio/wav", "audio/x-wav"}
)


def sniff_audio_mime_type(data: bytes) -> str | None:
    """Returns the format actually present in the bytes, or None if
    unrecognized. Ignores any claimed/declared MIME type entirely."""
    if data.startswith(_OGG_SIGNATURE):
        return "audio/ogg"
    if data.startswith(_ID3_SIGNATURE):
        return "audio/mpeg"
    if data.startswith(_AMR_WB_SIGNATURE):
        return "audio/amr-wb"
    if data.startswith(_AMR_NB_SIGNATURE):
        return "audio/amr"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "audio/mp4"
    # Raw MPEG frame sync (no ID3 tag) — 0xFF followed by a byte whose top 3
    # bits are all set (11 sync bits total).
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return None


def _probe_duration_seconds(data: bytes) -> float:
    """Raises MediaValidationError('corrupt_audio') if the container can't
    be opened/parsed at all — never silently reports 0 for genuinely corrupt
    input."""
    try:
        container = av.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - PyAV raises its own exception hierarchy per-format
        raise MediaValidationError("corrupt_audio") from exc

    try:
        if container.duration is not None:
            return container.duration / 1_000_000  # AV_TIME_BASE is microseconds

        for stream in container.streams.audio:
            if stream.duration is not None:
                return float(stream.duration * stream.time_base)

        raise MediaValidationError("corrupt_audio")
    finally:
        container.close()


def validate_audio(data: bytes, max_duration_seconds: float) -> tuple[str, float]:
    """Validates that `data` is a genuinely well-formed, supported audio
    file within the configured duration ceiling. Returns (sniffed_mime_type,
    duration_seconds) on success. Raises MediaValidationError on any
    failure — never returns a partial/best-effort result."""
    actual_mime = sniff_audio_mime_type(data)
    if actual_mime is None or actual_mime not in SUPPORTED_AUDIO_MIME_TYPES:
        raise MediaValidationError("unsupported_format")

    duration_seconds = _probe_duration_seconds(data)

    if duration_seconds <= 0:
        raise MediaValidationError("corrupt_audio")
    if duration_seconds > max_duration_seconds:
        raise MediaValidationError("duration_too_long")

    return actual_mime, duration_seconds
