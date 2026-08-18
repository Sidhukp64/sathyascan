"""
Shared claim-investigation / persistence / usage-accounting logic, used by
both TextPipeline (Phase 2) and ImagePipeline (Phase 3).

Extracted from TextPipeline verbatim in Phase 3 — behavior is unchanged,
this is a pure mechanical extraction so ImagePipeline can reuse the exact
same evidence-investigation, classification, persistence, and budget-
accounting logic instead of duplicating it (per the Phase 3 instruction to
reuse, not re-implement, existing infrastructure). Phase 2's full test
suite passing unchanged after this extraction is the verification that
nothing about the logic itself changed.
"""

import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget_guard import AnalysisBudgetGuard, BudgetExceededError
from app.agent.classification import classify_completed_investigation, classify_incomplete_investigation
from app.agent.investigation_loop import run_investigation_loop
from app.agent.schemas import ClaimVerdict, EvidenceItem, IncompleteReason
from app.agent.tools.evidence_search import EvidenceSearchTool
from app.agent.tools.evidence_synthesis import EvidenceSynthesisTool
from app.agent.usage_tracking import record_usage
from app.core.config import Settings
from app.core.provider_health import provider_health
from app.integrations.claude_client import LLMClient, LLMProviderError, LLMProviderTimeout
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.source_credibility import TIER_3_OTHER, tier_rank

_RESULT_PRIORITY = [
    "false",
    "misleading",
    "insufficient_evidence",
    "unverified",
    "partially_true",
    "verified",
    "opinion",
    "satire",
]


def aggregate_overall_result(verdicts: list[ClaimVerdict]) -> str:
    """Used by both TextPipeline and ImagePipeline to roll up a multi-claim
    analysis's per-claim results into one analyses.overall_result."""
    present = {v.result for v in verdicts}
    for candidate in _RESULT_PRIORITY:
        if candidate in present:
            return candidate
    return "unverified"


async def investigate_claim(
    llm: LLMClient,
    settings: Settings,
    claim_text: str,
    category: str,
    budget_guard: AnalysisBudgetGuard,
    evidence_search_tool: EvidenceSearchTool,
    synthesis_tool: EvidenceSynthesisTool,
    language: str,
) -> ClaimVerdict:
    investigation = await run_investigation_loop(llm, evidence_search_tool, budget_guard, claim_text)

    if not investigation.investigation_complete:
        reason: IncompleteReason = investigation.incomplete_reason or "other"
        return classify_incomplete_investigation(claim_text, category, reason, investigation.evidence)

    try:
        budget_guard.record_llm_call()
    except BudgetExceededError:
        return classify_incomplete_investigation(claim_text, category, "tool_call_limit", investigation.evidence)

    # Phase 9 — provider health tracking (roadmap §9.5); see
    # app/core/provider_health.py's docstring.
    _synthesis_started_at = time.monotonic()
    try:
        synthesis = await synthesis_tool.synthesize(claim_text, investigation.evidence, language)
        provider_health.record_success("llm", (time.monotonic() - _synthesis_started_at) * 1000)
    except LLMProviderTimeout as exc:
        provider_health.record_failure("llm", type(exc).__name__)
        return classify_incomplete_investigation(claim_text, category, "timeout", investigation.evidence)
    except LLMProviderError as exc:
        provider_health.record_failure("llm", type(exc).__name__)
        return classify_incomplete_investigation(claim_text, category, "provider_unavailable", investigation.evidence)

    # Merge the LLM's per-evidence stance judgments back onto the evidence
    # items BEFORE the deterministic guard reads them.
    for item, stance in zip(investigation.evidence, synthesis.evidence_stances, strict=False):
        item.stance = stance

    return classify_completed_investigation(
        claim_text,
        category,
        investigation.evidence,
        synthesis,
        settings.high_impact_categories_set,
        settings.min_concurring_tier2_sources,
    )


async def persist_claim(session: AsyncSession, analysis_id, order: int, verdict: ClaimVerdict) -> None:
    claim = Claim(
        analysis_id=analysis_id,
        claim_text=verdict.claim_text,
        claim_order=order,
        result=verdict.result,
        investigation_complete=verdict.investigation_complete,
        incomplete_reason=verdict.incomplete_reason,
        claim_confidence=verdict.claim_confidence,
        evidence_strength=verdict.evidence_strength,
        reasoning_text=verdict.reasoning_text,
        category=verdict.category,
        evidence_tier_met=verdict.evidence_tier_met,
        policy_override_applied=verdict.policy_override_applied,
    )
    session.add(claim)
    await session.flush()

    retrieved_at = datetime.now(timezone.utc)
    for item in verdict.evidence:
        session.add(
            Evidence(
                claim_id=claim.id,
                source_url=item.url or None,
                source_domain=item.domain or None,
                source_title=item.title or None,
                stance=item.stance or "neutral",
                relevance_score=item.relevance_score,
                credibility_tier=item.credibility_tier,
                snippet_text=(item.snippet or "")[:1000],  # snippet-only cap (decisions.md §3)
                retrieved_at=retrieved_at,
            )
        )
    await session.flush()


async def finalize_usage(
    session: AsyncSession,
    settings: Settings,
    analysis: Analysis,
    budget_guard: AnalysisBudgetGuard,
    started_at: datetime,
) -> None:
    analysis.evidence_search_count = budget_guard.state.evidence_search_count
    analysis.llm_tool_call_count = budget_guard.state.llm_tool_call_count
    analysis.budget_limit_hit = budget_guard.searches_remaining == 0 or budget_guard.tool_calls_remaining == 0
    analysis.processing_duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

    await record_usage(
        session,
        budget_guard.state.evidence_search_count,
        budget_guard.state.llm_tool_call_count,
        settings.estimated_cost_per_search_usd,
        settings.estimated_cost_per_llm_call_usd,
        settings.daily_spend_circuit_breaker_usd,
    )
    await session.flush()


async def load_verdicts(session: AsyncSession, analysis_id) -> list[ClaimVerdict]:
    claims_result = await session.execute(
        select(Claim).where(Claim.analysis_id == analysis_id).order_by(Claim.claim_order)
    )
    claims = claims_result.scalars().all()

    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        evidence_result = await session.execute(select(Evidence).where(Evidence.claim_id == claim.id))
        evidence_rows = evidence_result.scalars().all()
        evidence_items = [
            EvidenceItem(
                title=e.source_title or "",
                url=e.source_url or "",
                domain=e.source_domain or "",
                snippet=e.snippet_text or "",
                credibility_tier=e.credibility_tier or TIER_3_OTHER,
                tier_rank=tier_rank(e.credibility_tier),
                stance=e.stance if e.stance in ("supporting", "contradicting", "neutral") else None,
            )
            for e in evidence_rows
        ]
        verdicts.append(
            ClaimVerdict(
                claim_text=claim.claim_text,
                category=claim.category or "other",
                result=claim.result,
                investigation_complete=claim.investigation_complete,
                incomplete_reason=claim.incomplete_reason,
                claim_confidence=float(claim.claim_confidence) if claim.claim_confidence is not None else None,
                evidence_strength=float(claim.evidence_strength) if claim.evidence_strength is not None else None,
                evidence_tier_met=claim.evidence_tier_met,
                reasoning_text=claim.reasoning_text,
                policy_override_applied=claim.policy_override_applied,
                evidence=evidence_items,
            )
        )
    return verdicts
