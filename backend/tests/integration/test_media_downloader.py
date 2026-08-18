"""
MetaMediaClient tests — exercises the REAL download logic (not a fake
wrapper) via httpx.MockTransport, so SSRF checking, redirect refusal, size
enforcement, and error mapping are all tested against actual code paths.

resolve_and_check_hostname (real DNS resolution) is mocked here so these
tests are deterministic and offline — SSRF-check logic itself is covered
separately and thoroughly in test_ssrf_guard.py.
"""

from unittest.mock import patch

import httpx
import pytest

from app.core.ssrf_guard import SSRFBlockedError
from app.media.downloader import (
    MediaDownloadError,
    MediaDownloadTimeout,
    MediaTooLargeError,
    MetaMediaClient,
    UnsafeMediaURLError,
)


def _client(handler, max_size_bytes: int = 1_000_000) -> MetaMediaClient:
    transport = httpx.MockTransport(handler)
    return MetaMediaClient(
        graph_base_url="https://graph.facebook.com/v21.0",
        access_token="test-token",
        timeout_seconds=5.0,
        max_size_bytes=max_size_bytes,
        transport=transport,
    )


def _metadata_response(media_url: str, mime_type: str = "image/jpeg") -> httpx.Response:
    return httpx.Response(200, json={"url": media_url, "mime_type": mime_type, "id": "media-123"})


class TestValidDownload:
    async def test_downloads_valid_media(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://safe-cdn.example.com/file.jpg")
            return httpx.Response(200, content=b"fake-image-bytes", headers={"content-length": "17"})

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            data, claimed_mime = await _client(handler).download_media("media-123")

        assert data == b"fake-image-bytes"
        assert claimed_mime == "image/jpeg"


class TestUnsafeURL:
    async def test_ssrf_blocked_hostname_is_refused_before_any_download_request(self):
        download_was_attempted = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal download_was_attempted
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://internal-service.example/file.jpg")
            download_was_attempted = True
            return httpx.Response(200, content=b"should never be reached")

        with patch("app.media.downloader.resolve_and_check_hostname", side_effect=SSRFBlockedError("blocked")):
            with pytest.raises(UnsafeMediaURLError):
                await _client(handler).download_media("media-123")

        assert download_was_attempted is False

    async def test_non_https_media_url_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _metadata_response("http://insecure.example/file.jpg")  # not https

        with pytest.raises(UnsafeMediaURLError):
            await _client(handler).download_media("media-123")

    async def test_missing_url_in_metadata_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"mime_type": "image/jpeg"})  # no "url" key

        with pytest.raises(UnsafeMediaURLError):
            await _client(handler).download_media("media-123")


class TestRedirectAbuse:
    async def test_redirect_response_is_refused_not_followed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://cdn.example.com/file.jpg")
            return httpx.Response(302, headers={"location": "https://attacker.example/evil"})

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(UnsafeMediaURLError):
                await _client(handler).download_media("media-123")


class TestOversizedFile:
    async def test_declared_content_length_over_limit_is_rejected_before_reading_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://cdn.example.com/big.jpg")
            return httpx.Response(200, content=b"x" * 100, headers={"content-length": "999999999"})

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(MediaTooLargeError):
                await _client(handler, max_size_bytes=1000).download_media("media-123")

    async def test_actual_size_over_limit_is_rejected_even_without_content_length_header(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://cdn.example.com/big.jpg")
            return httpx.Response(200, content=b"x" * 5000)  # no content-length header

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(MediaTooLargeError):
                await _client(handler, max_size_bytes=1000).download_media("media-123")


class TestTimeoutAndTruncation:
    async def test_metadata_timeout_maps_to_media_download_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        with pytest.raises(MediaDownloadTimeout):
            await _client(handler).download_media("media-123")

    async def test_download_timeout_maps_to_media_download_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://cdn.example.com/file.jpg")
            raise httpx.TimeoutException("timed out mid-download")

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(MediaDownloadTimeout):
                await _client(handler).download_media("media-123")

    async def test_connection_dropped_during_download_maps_to_media_download_error(self):
        """Simulates a hard connection failure during the download request
        (the closest MockTransport can represent a truncated/dropped
        stream without a real socket) — must map to a clean, catchable
        MediaDownloadError, not propagate a raw httpx exception."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://cdn.example.com/file.jpg")
            raise httpx.ReadError("connection reset by peer")

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(MediaDownloadError):
                await _client(handler).download_media("media-123")


class TestClaimedMimeTypeIsUntrusted:
    async def test_claimed_mime_type_is_returned_as_is_never_validated_here(self):
        """The downloader passes through whatever Meta claims — validation
        of the ACTUAL content happens separately in media/validation.py,
        never trusted from this layer."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media-123"):
                return _metadata_response("https://cdn.example.com/file.jpg", mime_type="image/jpeg")
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"actually a png")  # mismatched!

        with patch("app.media.downloader.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            data, claimed_mime = await _client(handler).download_media("media-123")

        assert claimed_mime == "image/jpeg"  # the (wrong) claim, returned untouched
        assert data.startswith(b"\x89PNG")  # the real bytes — caller must sniff, not trust claimed_mime