"""
Media validation tests (app/media/validation.py) — never trusts claimed
MIME type or file extension, only magic bytes + actual decode.
"""

import io

from PIL import Image

from app.media.validation import MediaValidationError, sniff_image_mime_type, validate_image


def _jpeg_bytes(width: int = 50, height: int = 50) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes(width: int = 50, height: int = 50) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class TestSniffImageMimeType:
    def test_detects_jpeg(self):
        assert sniff_image_mime_type(_jpeg_bytes()) == "image/jpeg"

    def test_detects_png(self):
        assert sniff_image_mime_type(_png_bytes()) == "image/png"

    def test_returns_none_for_unrecognized_bytes(self):
        assert sniff_image_mime_type(b"not an image at all") is None

    def test_ignores_claimed_extension_entirely(self):
        # sniff_image_mime_type only ever looks at bytes — there is no
        # filename/extension parameter to even pass one in, by design.
        jpeg_data = _jpeg_bytes()
        assert sniff_image_mime_type(jpeg_data) == "image/jpeg"


class TestValidateImage:
    def test_accepts_valid_jpeg(self):
        actual_mime = validate_image(_jpeg_bytes(), max_pixels=1_000_000)
        assert actual_mime == "image/jpeg"

    def test_accepts_valid_png(self):
        actual_mime = validate_image(_png_bytes(), max_pixels=1_000_000)
        assert actual_mime == "image/png"

    def test_rejects_unsupported_format(self):
        try:
            validate_image(b"%PDF-1.4 fake pdf content", max_pixels=1_000_000)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "unsupported_format"

    def test_rejects_corrupt_image_data(self):
        # Valid JPEG magic bytes, but truncated/garbage payload after them.
        corrupt = b"\xff\xd8\xff" + b"\x00" * 50
        try:
            validate_image(corrupt, max_pixels=1_000_000)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason in {"corrupt_image", "processing_error"}

    def test_rejects_image_exceeding_pixel_limit(self):
        # decompression-bomb guard: a real, validly-decodable image, but
        # larger (in pixel count) than the configured ceiling.
        oversized = _jpeg_bytes(width=200, height=200)  # 40,000 pixels
        try:
            validate_image(oversized, max_pixels=1000)
            assert False, "expected MediaValidationError"
        except MediaValidationError as exc:
            assert exc.reason == "image_too_large_pixels"

    def test_accepts_image_within_pixel_limit(self):
        small = _jpeg_bytes(width=10, height=10)  # 100 pixels
        assert validate_image(small, max_pixels=1000) == "image/jpeg"

    def test_never_trusts_claimed_mime_type(self):
        """A PNG's real bytes, even if a caller claims elsewhere it's a
        JPEG — validate_image has no mime_type parameter to even accept a
        claim, so this is enforced by the function signature itself."""
        png_data = _png_bytes()
        assert validate_image(png_data, max_pixels=1_000_000) == "image/png"
