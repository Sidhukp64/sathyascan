"""
robots.txt compliance for direct URL fetches (decisions.md §3: "robots.txt
respected for direct URL fetches in the URL Analyzer").

Fetches robots.txt from the SAME host already validated by the caller's
SSRF check (SecureUrlFetcher._check_ssrf runs before this is called) — no
separate SSRF check needed here since we're not following any NEW host, only
a fixed `/robots.txt` path on a host already resolved-and-checked this
request. If robots.txt itself can't be fetched or parsed (missing, times
out, malformed), we default to ALLOWED — matching standard crawler
behavior: inability to retrieve robots.txt is conventionally treated as
"no restrictions stated," not a block.
"""

import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.core.logging import log_event

logger = logging.getLogger(__name__)


async def is_allowed_by_robots_txt(
    url: str,
    user_agent: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    parser = RobotFileParser()
    parser.set_url(robots_url)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.get(robots_url)
    except httpx.HTTPError:
        log_event(logger, logging.INFO, "robots.txt fetch failed, defaulting to allowed", robots_url=robots_url)
        return True

    if response.status_code >= 400:
        # No robots.txt published (404) or inaccessible — permissive default,
        # same as real-world crawler behavior.
        return True

    try:
        parser.parse(response.text.splitlines())
    except Exception:  # noqa: BLE001 - malformed robots.txt must never block a fetch
        log_event(logger, logging.INFO, "robots.txt could not be parsed, defaulting to allowed", robots_url=robots_url)
        return True

    return parser.can_fetch(user_agent, url)
