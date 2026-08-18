"""
SecureUrlFetcher tests — exercises the REAL fetch/redirect/SSRF logic (not a
fake wrapper) via httpx.MockTransport, same pattern as
test_media_downloader.py. resolve_and_check_hostname (real DNS resolution)
is mocked here for determinism — SSRF-check logic itself is covered
separately in test_ssrf_guard.py.
"""

from unittest.mock import patch

import httpx
import pytest

from app.core.ssrf_guard import SSRFBlockedError
from app.web.fetcher import (
    SecureUrlFetcher,
    TooManyRedirectsError,
    UnsafeUrlError,
    UrlFetchError,
    UrlFetchTimeout,
    UrlTooLargeError,
)


def _fetcher(handler, max_size_bytes: int = 1_000_000, max_redirects: int = 5) -> SecureUrlFetcher:
    transport = httpx.MockTransport(handler)
    return SecureUrlFetcher(
        timeout_seconds=5.0,
        max_size_bytes=max_size_bytes,
        max_redirects=max_redirects,
        user_agent="SathyaScanBot/1.0",
        transport=transport,
    )


def _ok(text: str = "<html><body>ok</body></html>") -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/html"})


class TestValidFetch:
    async def test_fetches_valid_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _ok("<html><body>hello</body></html>")

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            result = await _fetcher(handler).fetch("https://example.com/article")

        assert result.status_code == 200
        assert b"hello" in result.content_bytes
        assert result.content_type == "text/html"
        assert result.redirect_chain == []
        assert result.hop_count == 0


class TestSSRFProtection:
    async def test_ssrf_blocked_hostname_is_refused_before_any_request(self):
        request_made = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_made
            request_made = True
            return _ok()

        with patch("app.web.fetcher.resolve_and_check_hostname", side_effect=SSRFBlockedError("blocked")):
            with pytest.raises(UnsafeUrlError):
                await _fetcher(handler).fetch("https://internal.example/secret")

        assert request_made is False

    async def test_non_http_scheme_is_refused(self):
        with pytest.raises(UnsafeUrlError):
            await _fetcher(lambda r: _ok()).fetch("ftp://example.com/file")

    async def test_redirect_to_ssrf_blocked_host_is_refused_mid_chain(self):
        """The FIRST hop is safe, but it redirects to an internal address —
        must be blocked on the SECOND hop's own SSRF check, not just the
        first URL's."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "safe.example" in str(request.url):
                return httpx.Response(302, headers={"location": "https://internal.example/secret"})
            return _ok()

        def resolve_side_effect(hostname):
            if hostname == "internal.example":
                raise SSRFBlockedError("blocked")
            return ["93.184.216.34"]

        with patch("app.web.fetcher.resolve_and_check_hostname", side_effect=resolve_side_effect):
            with pytest.raises(UnsafeUrlError) as exc_info:
                await _fetcher(handler).fetch("https://safe.example/link")

        assert exc_info.value.blocked_host == "internal.example"


class TestRedirectFollowing:
    async def test_follows_redirect_and_records_chain(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "short.example" in str(request.url):
                return httpx.Response(301, headers={"location": "https://real-article.example/full"})
            return _ok("<html><body>full article</body></html>")

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            result = await _fetcher(handler).fetch("https://short.example/abc")

        assert result.final_url == "https://real-article.example/full"
        assert result.hop_count == 1
        assert result.redirect_chain[0].url == "https://short.example/abc"
        assert result.redirect_chain[0].status_code == 301
        assert result.crossed_domains is True
        assert b"full article" in result.content_bytes

    async def test_same_domain_redirect_does_not_count_as_crossed_domains(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/old"):
                return httpx.Response(301, headers={"location": "https://example.com/new"})
            return _ok()

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            result = await _fetcher(handler).fetch("https://example.com/old")

        assert result.crossed_domains is False

    async def test_relative_redirect_location_is_resolved_against_current_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://example.com/old":
                return httpx.Response(302, headers={"location": "/new"})
            return _ok()

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            result = await _fetcher(handler).fetch("https://example.com/old")

        assert result.final_url == "https://example.com/new"

    async def test_exceeding_max_redirects_raises(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(302, headers={"location": f"https://example.com/hop{call_count}"})

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(TooManyRedirectsError):
                await _fetcher(handler, max_redirects=3).fetch("https://example.com/start")

    async def test_redirect_missing_location_header_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)  # no Location header

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(UnsafeUrlError):
                await _fetcher(handler).fetch("https://example.com/broken-redirect")


class TestSizeLimits:
    async def test_declared_content_length_over_limit_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 100, headers={"content-length": "999999999"})

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(UrlTooLargeError):
                await _fetcher(handler, max_size_bytes=1000).fetch("https://example.com/huge")

    async def test_actual_size_over_limit_is_rejected_without_content_length_header(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 5000)

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(UrlTooLargeError):
                await _fetcher(handler, max_size_bytes=1000).fetch("https://example.com/huge")


class TestTimeoutAndErrors:
    async def test_timeout_maps_to_url_fetch_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(UrlFetchTimeout):
                await _fetcher(handler).fetch("https://example.com/slow")

    async def test_connection_dropped_maps_to_url_fetch_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("connection reset")

        with patch("app.web.fetcher.resolve_and_check_hostname", return_value=["93.184.216.34"]):
            with pytest.raises(UrlFetchError):
                await _fetcher(handler).fetch("https://example.com/dropped")
