"""
is_allowed_by_robots_txt tests — exercises the REAL parsing logic via
httpx.MockTransport, same pattern as test_media_downloader.py.
"""

import httpx

from app.web.robots import is_allowed_by_robots_txt

_UA = "SathyaScanBot/1.0 (+https://sathyascan.example/bot)"


def _transport(handler) -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(handler)


class TestRobotsAllowed:
    async def test_no_robots_txt_defaults_to_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True

    async def test_permissive_robots_txt_allows(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True


class TestRobotsDisallowed:
    async def test_disallowed_path_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/private/secret", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is False

    async def test_disallow_all_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is False

    async def test_allowed_path_outside_disallowed_scope_is_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/public/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True


class TestRobotsFailureModesDefaultToAllowed:
    async def test_connection_error_defaults_to_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True

    async def test_timeout_defaults_to_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True

    async def test_server_error_defaults_to_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True

    async def test_malformed_robots_txt_defaults_to_allowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # Binary garbage that a robots parser can't sensibly interpret.
            return httpx.Response(200, content=b"\x00\x01\xff\xfe not really robots.txt")

        allowed = await is_allowed_by_robots_txt(
            "https://example.com/article", _UA, 5.0, transport=_transport(handler)
        )
        assert allowed is True
