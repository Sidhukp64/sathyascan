"""
Secure media download from WhatsApp (whatsapp-integration.md's documented
flow, now actually implemented):

  media_id -> authenticated Meta metadata request -> validate returned URL
  -> SSRF protection -> download with strict limits (no redirects, size cap
  enforced both from Content-Length and mid-stream) -> caller validates
  actual content (media/validation.py) -> in-memory only, nothing persisted
  to disk (see app/agent/image_pipeline.py for why: "secure cleanup" holds
  by construction when nothing is ever written).

Nothing here trusts: the file extension, the claimed mime_type, the media
URL string alone (SSRF-checked), or the declared Content-Length (still
enforced mid-stream in case a server lies about it).
"""

from urllib.parse import urlparse

import httpx

from app.core.ssrf_guard import SSRFBlockedError, resolve_and_check_hostname

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class MediaDownloadError(Exception):
    """Maps to incomplete_reason='infra_error' upstream (decisions.md §1A)."""


class MediaDownloadTimeout(MediaDownloadError):
    """Maps to incomplete_reason='timeout'."""


class MediaTooLargeError(MediaDownloadError):
    """Maps to a friendly PRD §38 'file too large' reply, not a claim outcome
    — no analysis has started yet at this point."""


class UnsafeMediaURLError(MediaDownloadError):
    """SSRF block, non-HTTPS URL, or an attempted redirect. Never exposed to
    the user in detail (decisions.md §15) — logged, generic reply only."""


class MetaMediaClient:
    def __init__(
        self,
        graph_base_url: str,
        access_token: str,
        timeout_seconds: float,
        max_size_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._graph_base_url = graph_base_url
        self._access_token = access_token
        self._timeout_seconds = timeout_seconds
        self._max_size_bytes = max_size_bytes
        # Test-only injection point (httpx.MockTransport) — None means the
        # real network transport, unchanged from before this parameter existed.
        self._transport = transport

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def fetch_metadata(self, media_id: str) -> dict:
        url = f"{self._graph_base_url}/{media_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = await client.get(url, headers=self._auth_headers())
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MediaDownloadTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise MediaDownloadError(str(exc)) from exc
        return response.json()

    def _validate_and_resolve_url(self, media_url: str | None) -> str:
        if not media_url or not media_url.startswith("https://"):
            raise UnsafeMediaURLError("media URL missing or not https")

        hostname = urlparse(media_url).hostname
        if not hostname:
            raise UnsafeMediaURLError("could not parse media URL host")

        try:
            resolve_and_check_hostname(hostname)
        except SSRFBlockedError as exc:
            raise UnsafeMediaURLError(str(exc)) from exc

        return media_url

    async def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        """Returns (raw_bytes, claimed_mime_type). claimed_mime_type is
        UNTRUSTED — always sniff the real type via media/validation.py before
        acting on it."""
        metadata = await self.fetch_metadata(media_id)
        claimed_mime_type = metadata.get("mime_type")
        media_url = self._validate_and_resolve_url(metadata.get("url"))

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, follow_redirects=False, transport=self._transport
            ) as client:
                async with client.stream("GET", media_url, headers=self._auth_headers()) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        raise UnsafeMediaURLError("media host attempted a redirect, refused")
                    response.raise_for_status()

                    declared_size = response.headers.get("content-length")
                    if declared_size is not None and int(declared_size) > self._max_size_bytes:
                        raise MediaTooLargeError(f"declared size {declared_size} exceeds limit")

                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > self._max_size_bytes:
                            # Abort mid-stream rather than reading an
                            # arbitrarily large body to completion first.
                            raise MediaTooLargeError("downloaded size exceeded limit mid-stream")
        except httpx.TimeoutException as exc:
            raise MediaDownloadTimeout(str(exc)) from exc
        except (MediaTooLargeError, UnsafeMediaURLError):
            raise
        except httpx.HTTPError as exc:
            # Covers connection drops / truncated streams (httpx.ReadError,
            # RemoteProtocolError, etc. — all subclasses of HTTPError).
            raise MediaDownloadError(str(exc)) from exc

        return bytes(buffer), claimed_mime_type
