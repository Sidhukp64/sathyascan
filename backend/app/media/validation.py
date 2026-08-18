"""
Image content validation. Never trusts a caller-supplied MIME type or file
extension — the actual format is sniffed from magic bytes, and Pillow is
used only to check dimensions (decompression-bomb guard) and structural
validity, never to trust metadata the file itself claims about itself.

MediaValidationError below is intentionally generic (not image-specific) —
Phase 5's app/media/audio_validation.py and app/media/video_validation.py
both reuse it directly rather than defining parallel exception classes, each
contributing their own `reason` values to the same shared type.
"""

import io

from PIL import Image, UnidentifiedImageError

# Magic-byte signatures for the formats WhatsApp actually sends (JPEG/PNG
# for photos and screenshots, WEBP for stickers, GIF is rare but cheap to
# support). Anything else is rejected regardless of claimed mime_type/extension.
_MAGIC_SIGNATURES: dict[bytes, str] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}

SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


class MediaValidationError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        # image: unsupported_format | corrupt_image | image_too_large_pixels | processing_error
        # audio (app/media/audio_validation.py): unsupported_format | corrupt_audio |
        #   duration_too_long | processing_error
        # video (app/media/video_validation.py): unsupported_format | corrupt_video |
        #   duration_too_long | resolution_too_large | processing_error
        super().__init__(reason)


def sniff_image_mime_type(data: bytes) -> str | None:
    """Returns the format actually present in the bytes, or None if
    unrecognized. Ignores any claimed/declared MIME type entirely."""
    for magic, mime in _MAGIC_SIGNATURES.items():
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, max_pixels: int) -> str:
    """Validates that `data` is a genuinely well-formed, supported image
    within the configured pixel-count ceiling (decompression-bomb guard).
    Returns the sniffed (real) MIME type on success. Raises
    MediaValidationError on any failure — never returns a partial/best-effort
    result."""
    actual_mime = sniff_image_mime_type(data)
    if actual_mime is None or actual_mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise MediaValidationError("unsupported_format")

    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width <= 0 or height <= 0:
                raise MediaValidationError("corrupt_image")
            if width * height > max_pixels:
                raise MediaValidationError("image_too_large_pixels")
            # verify() raises on structurally corrupt/truncated image data.
            # Per Pillow's own docs the image object must not be reused for
            # further loading after verify() — we don't need to, we're only
            # validating here; OCR/analysis stages re-open a fresh copy.
            img.verify()
    except MediaValidationError:
        raise
    except UnidentifiedImageError as exc:
        raise MediaValidationError("corrupt_image") from exc
    except Image.DecompressionBombError as exc:
        raise MediaValidationError("image_too_large_pixels") from exc
    except Exception as exc:  # noqa: BLE001 - any other Pillow decode failure
        raise MediaValidationError("processing_error") from exc

    return actual_mime
