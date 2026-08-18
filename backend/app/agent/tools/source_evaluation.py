"""
Source Evaluation tool (agent-architecture.md's Source Validator contract:
{domain} -> {credibility_tier, region, notes}).

Deterministic DB lookup, NOT a Claude-callable function — decisions.md §3 is
explicit that credibility comes only from the registry, never an LLM
judgment. The orchestrator calls this automatically on every search result;
Claude never "asks" for it.
"""

from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_credibility import TIER_3_OTHER, SourceCredibilityRegistry, tier_rank


def extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc.removeprefix("www.")


class SourceEvaluationTool:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def evaluate(self, domain: str) -> tuple[str, int | None]:
        """Returns (credibility_tier, tier_rank). Unknown domains default to
        tier_3_other (decisions.md §3) — never inferred from search rank,
        because this function never sees search rank at all."""
        if not domain:
            return TIER_3_OTHER, tier_rank(TIER_3_OTHER)

        result = await self._session.execute(
            select(SourceCredibilityRegistry).where(SourceCredibilityRegistry.domain == domain)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return TIER_3_OTHER, tier_rank(TIER_3_OTHER)
        return row.credibility_tier, tier_rank(row.credibility_tier)
