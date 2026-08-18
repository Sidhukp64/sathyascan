"""
SecureUrlFetcher — SSRF-protected fetch of an arbitrary, user-submitted URL
(decisions.md §15), for the URL Analyzer / URL Safety Module.

Deliberately a DIFFERENT redirect policy from app/media/downloader.py's
Meta-CDN download (which refuses all redirects outright): general web URLs
redirect constantly (shorteners, canonical-URL bounces), so this fetcher
FOLLOWS redirects up to a capped hop count — SSRF-checking the resolved IP
of EVERY hop before following it, never just the first URL — and returns
the full redirect chain so the caller (url_safety.py) can treat the
redirect PATTERN itself as a safety signal. Confirmed with the user as an
intentional Phase 4 policy choice, distinct from the media downloader's
refuse-all-redirects rule (which remains correct for Meta's narrow CDN use).

Nothing here trusts: the URL string alone (SSRF-checked via
app/core/ssrf_guard.py at every hop — resolved IP, not just the hostname
string), the declared Content-Length (still enforced mid-stream), or a
redirect Location header (re-validated exactly like the original URL, since
an open redirect on an otherwise-safe site must not be able to bounce this
fetcher at an internal address).
"""

import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from app.core.logging import log_event
from app.core.ssrf_guard import SSRFBlockedError, resolve_and_check_hostname

logger = logging.getLogger(__name__)

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_ALLOWED_SCHEMES = {"http", "https"}


class UrlFetchError(Exception):
    """Base class — maps to a generic processing-error reply upstream."""


class UnsafeUrlError(UrlFetchError):
    """SSRF block, non-http(s) scheme, or an unparseable host.

    Note this is NOT treated the same as decisions.md §6's illegal-content
    safety gate, which must never explain why it blocked something. An SSRF
    block here is ordinary, non-sensitive URL-safety information — the
    caller (app/agent/tools/url_safety.py) deliberately surfaces
    `blocked_host` to the user as a safety finding ("this link points to a
    private/internal address"), per the confirmed Phase 4 transparency
    policy.
    """

    def __init__(self, message: str, blocked_host: str | None = None) -> None:
        super().__init__(message)
        self.blocked_host = blocked_host


class UrlFetchTimeout(UrlFetchError):
    pass


class UrlTooLargeError(UrlFetchError):
    pass


class TooManyRedirectsError(UrlFetchError):
    pass


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status_code: int


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    status_code: int
    content_bytes: bytes
    content_type: str | None
    redirect_chain: list[RedirectHop] = field(default_factory=list)

    @property
    def hop_count(self) -> int:
        return len(self.redirect_chain)

    @property
    def crossed_domains(self) -> bool:
        """True if the final domain differs from the domain first requested
        — a URL Safety heuristic input, not a fetch-level concern."""
        if not self.redirect_chain:
            return False
        first_domain = urlparse(self.redirect_chain[0].url).hostname
        final_domain = urlparse(self.final_url).hostname
        return bool(first_domain and final_domain and first_domain.lower() != final_domain.lower())


def _validate_scheme_and_host(url: str) -> str:
    """Returns the validated hostname, or raises UnsafeUrlError."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeUrlError("could not parse URL host")
    return parsed.hostname


class SecureUrlFetcher:
    def __init__(
        self,
        timeout_seconds: float,
        max_size_bytes: int,
        max_redirects: int,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_size_bytes = max_size_bytes
        self._max_redirects = max_redirects
        self._user_agent = user_agent
        # Test-only injection point (httpx.MockTransport), same pattern as
        # app/media/downloader.py.
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent}

    def _check_ssrf(self, url: str) -> None:
        hostname = _validate_scheme_and_host(url)
        try:
            resolve_and_check_hostname(hostname)
        except SSRFBlockedError as exc:
            raise UnsafeUrlError(str(exc), blocked_host=hostname) from exc

    async def _fetch_one(self, url: str) -> tuple[FetchResult | None, int | None, str | None]:
        """Fetches exactly one hop. Returns (FetchResult, None, None) on a
        terminal (non-redirect) response, or (None, status_code, next_url)
        if this hop was itself a redirect."""
        self._check_ssrf(url)

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, follow_redirects=False, transport=self._transport
            ) as client:
                async with client.stream("GET", url, headers=self._headers()) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeUrlError("redirect response missing Location header")
                        next_url = urljoin(url, location)
                        return None, response.status_code, next_url

                    content_type = response.headers.get("content-type")
                    declared_size = response.headers.get("content-length")
                    if declared_size is not None and int(declared_size) > self._max_size_bytes:
                        raise UrlTooLargeError(f"declared size {declared_size} exceeds limit")

                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > self._max_size_bytes:
                            raise UrlTooLargeError("downloaded size exceeded limit mid-stream")

                    return (
                        FetchResult(
                            final_url=url,
                            status_code=response.status_code,
                            content_bytes=bytes(buffer),
                            content_type=content_type,
                            redirect_chain=[],  # filled in by fetch()
                        ),
                        None,
                        None,
                    )
        except httpx.TimeoutException as exc:
            raise UrlFetchTimeout(str(exc)) from exc
        except (UnsafeUrlError, UrlTooLargeError):
            raise
        except httpx.HTTPError as exc:
            raise UrlFetchError(str(exc)) from exc

    async def fetch(self, url: str) -> FetchResult:
        """Fetches `url`, following up to `max_redirects` hops. SSRF-checks
        the resolved IP of EVERY hop before following it — never just the
        first URL. `redirect_chain` on the result records each hop that
        REDIRECTED (the URL that returned the 3xx and its status code), in
        the order they were followed."""
        current_url = url
        redirect_chain: list[RedirectHop] = []

        for _hop_index in range(self._max_redirects + 1):
            result, status_code, next_url = await self._fetch_one(current_url)

            if result is not None:
                return FetchResult(
                    final_url=result.final_url,
                    status_code=result.status_code,
                    content_bytes=result.content_bytes,
                    content_type=result.content_type,
                    redirect_chain=redirect_chain,
                )

            redirect_chain.append(RedirectHop(url=current_url, status_code=status_code))  # type: ignore[arg-type]
            current_url = next_url  # type: ignore[assignment]

        log_event(logger, logging.WARNING, "too many redirects", hop_count=len(redirect_chain))
        raise TooManyRedirectsError(f"exceeded {self._max_redirects} redirects")
