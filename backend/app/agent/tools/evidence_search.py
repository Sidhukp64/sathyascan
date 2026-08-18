"""
Evidence Search tool — the ONE tool actually exposed to Claude's
function-calling during the bounded investigation loop (see
app/agent/investigation_loop.py). Executed by the orchestrator when Claude
requests it; Claude never calls the search provider directly.

Every result is immediately, automatically enriched with its credibility
tier via SourceEvaluationTool — the model receives tier information but
never assigns it (decisions.md §3).
"""

from typing import Any

from app.agent.schemas import EvidenceItem
from app.agent.tools.source_evaluation import SourceEvaluationTool, extract_domain
from app.integrations.evidence_search_client import EvidenceSearchProvider

TOOL_NAME = "search_evidence"


def tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Search for evidence relevant to the claim under investigation. Returns "
            "a list of sources with title, domain, credibility tier, and a short "
            "snippet. Call this as many times as needed with different, refined "
            "queries, but stop once you have enough evidence to judge the claim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A focused search query."}
            },
            "required": ["query"],
        },
    }


class EvidenceSearchTool:
    def __init__(self, search_provider: EvidenceSearchProvider, source_evaluator: SourceEvaluationTool) -> None:
        self._search_provider = search_provider
        self._source_evaluator = source_evaluator

    async def execute(self, query: str, max_results: int = 5) -> list[EvidenceItem]:
        results = await self._search_provider.search(query, max_results=max_results)
        items: list[EvidenceItem] = []
        for result in results:
            domain = extract_domain(result.url)
            tier, rank = await self._source_evaluator.evaluate(domain)
            items.append(
                EvidenceItem(
                    title=result.title,
                    url=result.url,
                    domain=domain,
                    snippet=result.snippet,
                    credibility_tier=tier,
                    tier_rank=rank,
                )
            )
        return items
