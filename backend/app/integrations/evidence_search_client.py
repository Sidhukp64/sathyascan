"""
Evidence search provider client. Snippet-only results (decisions.md §3 — no
full-text scraping); Tavily is the default provider (SEARCH_API_PROVIDER),
swappable behind the EvidenceSearchProvider Protocol per the "modular and
replaceable" NFR (agent-architecture.md).

No direct URL fetching happens here or anywhere in Phase 2 — this client
only calls the search provider's own API endpoint (fixed, trusted, not
attacker-influenced), which is why Phase 2 has no SSRF attack surface despite
the SSRF guard utility already existing (see core/ssrf_guard.py).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: datetime | None = None


class EvidenceSearchProviderError(Exception):
    """Maps to incomplete_reason='provider_unavailable' (decisions.md §1A)."""


class EvidenceSearchProviderTimeout(EvidenceSearchProviderError):
    """Maps to incomplete_reason='timeout' (decisions.md §1A)."""


class EvidenceSearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]: ...


class TavilyEvidenceSearchProvider:
    """Real implementation. Never imported by tests — see
    tests/conftest.py's FakeEvidenceSearchProvider."""

    _ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str, timeout_seconds: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(self._ENDPOINT, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise EvidenceSearchProviderTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise EvidenceSearchProviderError(str(exc)) from exc

        data = response.json()
        results = []
        for item in data.get("results", [])[:max_results]:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", "")[:1000],  # snippet-only, capped (decisions.md §3)
                    published_at=None,  # Tavily's basic response doesn't reliably include this
                )
            )
        return results
