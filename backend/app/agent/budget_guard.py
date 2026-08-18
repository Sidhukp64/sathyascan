"""
Per-analysis budget guard (decisions.md §1/§1A).

Hard, code-enforced ceilings on evidence searches and LLM/tool calls for a
single analysis. This is NOT a request to the model — the orchestrator
refuses to issue another tool call once a limit is hit, full stop. Hitting a
limit always resolves the current claim to insufficient_evidence; it is
never treated as evidence toward any other result (decisions.md §1A).
"""

from dataclasses import dataclass


class BudgetExceededError(Exception):
    """Raised by the guard when a caller tries to spend budget that isn't
    there. The orchestrator catches this and forces insufficient_evidence —
    it never lets the exception's presence/absence leak into the result in
    any other way."""

    def __init__(self, reason: str) -> None:
        self.reason = reason  # "search_limit" | "tool_call_limit"
        super().__init__(reason)


@dataclass
class BudgetState:
    evidence_search_count: int = 0
    llm_tool_call_count: int = 0


class AnalysisBudgetGuard:
    """One instance per analysis (covers all claims within it — the
    per-analysis limits in decisions.md §1 are analysis-wide, not per-claim)."""

    def __init__(self, max_evidence_searches: int, max_llm_tool_calls: int) -> None:
        self._max_searches = max_evidence_searches
        self._max_tool_calls = max_llm_tool_calls
        self.state = BudgetState()

    @property
    def searches_remaining(self) -> int:
        return max(0, self._max_searches - self.state.evidence_search_count)

    @property
    def tool_calls_remaining(self) -> int:
        return max(0, self._max_tool_calls - self.state.llm_tool_call_count)

    def record_llm_call(self) -> None:
        """Call once per Claude API call (structured calls AND each turn of
        the tool loop) — this is the LLM/tool-call counter, distinct from
        the evidence-search counter below."""
        if self.state.llm_tool_call_count >= self._max_tool_calls:
            raise BudgetExceededError("tool_call_limit")
        self.state.llm_tool_call_count += 1

    def record_evidence_search(self) -> None:
        if self.state.evidence_search_count >= self._max_searches:
            raise BudgetExceededError("search_limit")
        self.state.evidence_search_count += 1

    def can_search(self) -> bool:
        return self.searches_remaining > 0

    def can_call_llm(self) -> bool:
        return self.tool_calls_remaining > 0
