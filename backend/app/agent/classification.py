"""
Deterministic classification guard (decisions.md §1A, §3, §5).

This is the hard, code-enforced safety layer sitting on top of Claude's
judgment. The LLM (EvidenceSynthesisTool) judges each evidence item's
STANCE and SUGGESTS a result — genuine judgment calls (agent-architecture.md).
This module then independently recomputes, from evidence tiers alone,
whether the evidence actually clears the bar for that suggestion. On any
disagreement, the deterministic, evidence-grounded result wins — never the
model's. This is what makes "never produce a strong False/Verified verdict
without satisfying the configured evidence requirements" a property of the
code, not a prompt instruction the model could ignore (whether from being
wrong, or from a prompt-injection attempt in the claim text or evidence
content it was given).

Two entirely separate code paths, matching decisions.md §1A exactly:
  - classify_incomplete_investigation(): investigation was cut short
    (budget/tool/provider/timeout/infra) -> ALWAYS insufficient_evidence.
    No LLM call happens for this path at all (see orchestrator.py) — there
    is nothing for this module to override, by construction.
  - classify_completed_investigation(): investigation ran to completion ->
    apply the tier-bar guard described above.
"""

from app.agent.schemas import ClaimVerdict, EvidenceItem, IncompleteReason, SynthesisResult
from app.models.source_credibility import TIER_1_VALUES, TIER_2_VALUES  # noqa: F401  (documents the tier source)

_TIER_STRENGTH = {1: 0.9, 2: 0.65, 3: 0.3}
_DEFAULT_STRENGTH = 0.15
_OVERRIDE_CONFIDENCE_CAP = 0.5


def evidence_strength_for_tier(evidence_tier_met: int | None) -> float:
    if evidence_tier_met is None:
        return _DEFAULT_STRENGTH
    return _TIER_STRENGTH.get(evidence_tier_met, _DEFAULT_STRENGTH)


def _qualifying_tier(
    items: list[EvidenceItem],
    category: str,
    high_impact_categories: set[str],
    min_concurring_tier2: int,
) -> int | None:
    """Best (lowest-numbered) tier this evidence group qualifies at, or None
    if it doesn't clear the bar at all. Tier 1: any single source qualifies.
    Tier 2: needs >= min_concurring_tier2 concurring sources, UNLESS the
    claim's category is high-impact (decisions.md §5), in which case Tier 2
    never qualifies alone — only Tier 1 does."""
    if any(e.tier_rank == 1 for e in items):
        return 1

    if category in high_impact_categories:
        return None  # high-impact claims require Tier 1, no Tier-2 alternative

    tier2_count = sum(1 for e in items if e.tier_rank == 2)
    if tier2_count >= min_concurring_tier2:
        return 2

    return None


def _best_tier_seen(evidence: list[EvidenceItem]) -> int | None:
    ranks = [e.tier_rank for e in evidence if e.tier_rank is not None]
    return min(ranks) if ranks else None


def compute_deterministic_result(
    evidence: list[EvidenceItem],
    category: str,
    high_impact_categories: set[str],
    min_concurring_tier2: int,
) -> tuple[str, int | None]:
    """Returns (result, evidence_tier_met) computed ONLY from evidence
    tiers/stances — no LLM input. This is the ground truth the guard checks
    the LLM's suggestion against."""
    supporting = [e for e in evidence if e.stance == "supporting" and e.tier_rank is not None]
    contradicting = [e for e in evidence if e.stance == "contradicting" and e.tier_rank is not None]

    support_tier = _qualifying_tier(supporting, category, high_impact_categories, min_concurring_tier2)
    contra_tier = _qualifying_tier(contradicting, category, high_impact_categories, min_concurring_tier2)

    if support_tier is not None and contra_tier is None:
        return "verified", support_tier
    if contra_tier is not None and support_tier is None:
        return "false", contra_tier
    if support_tier is not None and contra_tier is not None:
        # Credible evidence conflicts — distinct from "no qualifying evidence
        # either way" (that's unverified). See module docstring.
        return "misleading", min(support_tier, contra_tier)

    return "unverified", _best_tier_seen(evidence)


def classify_incomplete_investigation(
    claim_text: str,
    category: str,
    incomplete_reason: IncompleteReason,
    evidence_gathered_so_far: list[EvidenceItem],
) -> ClaimVerdict:
    """decisions.md §1A: a budget/tool/provider/timeout/infra limit is NEVER
    evidence toward any result. Partial findings gathered before the cutoff
    are stored for transparency/audit but never influence the verdict —
    reasoning_text is a fixed template, not an LLM call, so nothing (not
    even a subtly-biased partial-evidence prompt) can sneak a lean in."""
    return ClaimVerdict(
        claim_text=claim_text,
        category=category,
        result="insufficient_evidence",
        investigation_complete=False,
        incomplete_reason=incomplete_reason,
        claim_confidence=None,
        evidence_strength=None,
        evidence_tier_met=None,
        reasoning_text=(
            "We weren't able to complete enough research to check this claim fully "
            f"({_reason_phrase(incomplete_reason)}). This does not mean the claim is "
            "true or false — we simply didn't gather enough evidence to say."
        ),
        policy_override_applied=False,
        evidence=evidence_gathered_so_far,
    )


def _reason_phrase(reason: IncompleteReason) -> str:
    return {
        "search_limit": "we reached our search limit for this request",
        "tool_call_limit": "we reached our processing limit for this request",
        "provider_unavailable": "one of our information services was unavailable",
        "timeout": "the research took too long and timed out",
        "infra_error": "an internal error interrupted the research",
        "other": "the research could not be completed",
    }[reason]


def classify_completed_investigation(
    claim_text: str,
    category: str,
    evidence: list[EvidenceItem],
    llm_synthesis: SynthesisResult,
    high_impact_categories: set[str],
    min_concurring_tier2: int,
) -> ClaimVerdict:
    deterministic_result, evidence_tier_met = compute_deterministic_result(
        evidence, category, high_impact_categories, min_concurring_tier2
    )

    policy_override_applied = False
    final_result = llm_synthesis.suggested_result
    final_confidence = llm_synthesis.claim_confidence

    if llm_synthesis.suggested_result != deterministic_result:
        # The model's suggestion doesn't match what the evidence tiers
        # actually support -> the deterministic, evidence-grounded result
        # always wins. This is the hard guard against both model error and
        # prompt injection (decisions.md §15, §1A).
        final_result = deterministic_result
        final_confidence = min(final_confidence, _OVERRIDE_CONFIDENCE_CAP)
        policy_override_applied = True

    return ClaimVerdict(
        claim_text=claim_text,
        category=category,
        result=final_result,
        investigation_complete=True,
        incomplete_reason=None,
        claim_confidence=round(final_confidence, 3),
        evidence_strength=round(evidence_strength_for_tier(evidence_tier_met), 3),
        evidence_tier_met=evidence_tier_met,
        reasoning_text=llm_synthesis.reasoning_text,
        policy_override_applied=policy_override_applied,
        evidence=evidence,
    )
