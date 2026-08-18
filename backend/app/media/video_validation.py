"""
Video content validation. Never trusts a caller-supplied MIME type or file
extension — the actual container format is sniffed from magic bytes, and
PyAV (`av`) is used only to probe genuine duration/resolution and structural
validity (a real video stream must actually be present), never to trust
metadata the file itself claims about itself. Mirrors
app/media/audio_validation.py's approach exactly, reusing the same shared
MediaValidationError.
"""

import io

import av

from app.media.validation import MediaValidationError

_WEBM_SIGNATURE = b"\x1a\x45\xdf\xa3"  # EBML header, used by both WebM and MKV

SUPPORTED_VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/3gpp", "video/webm", "video/quicktime"})


def sniff_video_mime_type(data: bytes) -> str | None:
    """Returns the format actually present in the bytes, or None if
    unrecognized. Ignores any claimed/declared MIME type entirely."""
    if data.startswith(_WEBM_SIGNATURE):
        return "video/webm"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "video/mp4"  # covers MP4/3GPP/MOV — all share the ftyp box; WhatsApp sends video/mp4 in practice
    return None


def _probe_video(data: bytes) -> tuple[float, int, int]:
    """Raises MediaValidationError('corrupt_video') if the container can't
    be opened/parsed, or has no video stream at all. Returns
    (duration_seconds, width, height)."""
    try:
        container = av.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - PyAV raises its own exception hierarchy per-format
        raise MediaValidationError("corrupt_video") from exc

    try:
        video_streams = container.streams.video
        if not video_streams:
            raise MediaValidationError("corrupt_video")
        video_stream = video_streams[0]

        if container.duration is not None:
            duration_seconds = container.duration / 1_000_000  # AV_TIME_BASE is microseconds
        elif video_stream.duration is not None:
            duration_seconds = float(video_stream.duration * video_stream.time_base)
        else:
            raise MediaValidationError("corrupt_video")

        width, height = video_stream.width, video_stream.height
        if not width or not height:
            raise MediaValidationError("corrupt_video")

        return duration_seconds, width, height
    finally:
        container.close()


def validate_video(
    data: bytes, max_duration_seconds: float, max_pixels: int
) -> tuple[str, float, int, int]:
    """Validates that `data` is a genuinely well-formed, supported video
    within the configured duration/resolution ceilings. Returns
    (sniffed_mime_type, duration_seconds, width, height) on success. Raises
    MediaValidationError on any failure — never returns a partial/best-effort
    result."""
    actual_mime = sniff_video_mime_type(data)
    if actual_mime is None or actual_mime not in SUPPORTED_VIDEO_MIME_TYPES:
        raise MediaValidationError("unsupported_format")

    duration_seconds, width, height = _probe_video(data)

    if duration_seconds <= 0:
        raise MediaValidationError("corrupt_video")
    if duration_seconds > max_duration_seconds:
        raise MediaValidationError("duration_too_long")
    if width * height > max_pixels:  # same decompression-bomb-style guard as images, per frame resolution
        raise MediaValidationError("resolution_too_large")

    return actual_mime, duration_seconds, width, height
