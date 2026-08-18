"""
Pure-logic tests for the deterministic classification guard — no mocking
needed, this module has no I/O. These are the most important tests in the
whole suite: they prove "never produce a strong False/Verified verdict
without satisfying the configured evidence requirements" is actually true
in code, independent of what any LLM says.
"""

from app.agent.classification import (
    classify_completed_investigation,
    classify_incomplete_investigation,
    compute_deterministic_result,
)
from app.agent.schemas import EvidenceItem, SynthesisResult

HIGH_IMPACT = {"elections", "gov_scheme"}
MIN_TIER2 = 2


def _evidence(stance, tier_rank, tier="tier_1_gov_official") -> EvidenceItem:
    return EvidenceItem(
        title="t", url="https://example.com/a", domain="example.com", snippet="s",
        credibility_tier=tier, tier_rank=tier_rank, stance=stance,
    )


class TestComputeDeterministicResult:
    def test_tier1_supporting_only_is_verified(self):
        evidence = [_evidence("supporting", 1)]
        result, tier = compute_deterministic_result(evidence, "other", HIGH_IMPACT, MIN_TIER2)
        assert result == "verified"
        assert tier == 1

    def test_tier1_contradicting_only_is_false(self):
        evidence = [_evidence("contradicting", 1)]
        result, tier = compute_deterministic_result(evidence, "other", HIGH_IMPACT, MIN_TIER2)
        assert result == "false"
        assert tier == 1

    def test_tier1_both_directions_is_misleading(self):
        evidence = [_evidence("supporting", 1), _evidence("contradicting", 1)]
        result, tier = compute_deterministic_result(evidence, "other", HIGH_IMPACT, MIN_TIER2)
        assert result == "misleading"
        assert tier == 1

    def test_two_concurring_tier2_supporting_is_verified_for_normal_category(self):
        evidence = [_evidence("supporting", 2, "tier_2_reputable_news"), _evidence("supporting", 2, "tier_2_reputable_news")]
        result, tier = compute_deterministic_result(evidence, "tech", HIGH_IMPACT, MIN_TIER2)
        assert result == "verified"
        assert tier == 2

    def test_single_tier2_source_is_not_enough(self):
        evidence = [_evidence("supporting", 2, "tier_2_reputable_news")]
        result, tier = compute_deterministic_result(evidence, "tech", HIGH_IMPACT, MIN_TIER2)
        assert result == "unverified"

    def test_high_impact_category_requires_tier1_even_with_concurring_tier2(self):
        evidence = [_evidence("supporting", 2, "tier_2_reputable_news"), _evidence("supporting", 2, "tier_2_reputable_news")]
        result, tier = compute_deterministic_result(evidence, "elections", HIGH_IMPACT, MIN_TIER2)
        assert result == "unverified"  # tier-2 never qualifies alone for high-impact categories

    def test_no_evidence_is_unverified_with_no_tier(self):
        result, tier = compute_deterministic_result([], "other", HIGH_IMPACT, MIN_TIER2)
        assert result == "unverified"
        assert tier is None

    def test_only_neutral_evidence_is_unverified(self):
        evidence = [_evidence("neutral", 1)]
        result, tier = compute_deterministic_result(evidence, "other", HIGH_IMPACT, MIN_TIER2)
        assert result == "unverified"
        assert tier == 1  # best tier SEEN, even though it didn't support a verdict


class TestClassifyCompletedInvestigation:
    def test_llm_agreement_is_accepted_as_is(self):
        evidence = [_evidence("supporting", 1)]
        synthesis = SynthesisResult(
            evidence_stances=["supporting"], suggested_result="verified", claim_confidence=0.9, reasoning_text="r"
        )
        verdict = classify_completed_investigation("claim", "other", evidence, synthesis, HIGH_IMPACT, MIN_TIER2)
        assert verdict.result == "verified"
        assert verdict.claim_confidence == 0.9
        assert verdict.policy_override_applied is False

    def test_llm_overclaims_verified_without_qualifying_evidence_is_downgraded(self):
        """The critical safety-guard test: even if the model insists on
        'verified', with no qualifying evidence the deterministic result
        (unverified) wins, and confidence is capped."""
        evidence = [_evidence("neutral", 3, "tier_3_other")]
        synthesis = SynthesisResult(
            evidence_stances=["neutral"], suggested_result="verified", claim_confidence=0.95, reasoning_text="r"
        )
        verdict = classify_completed_investigation("claim", "other", evidence, synthesis, HIGH_IMPACT, MIN_TIER2)
        assert verdict.result == "unverified"
        assert verdict.policy_override_applied is True
        assert verdict.claim_confidence <= 0.5

    def test_llm_overclaims_false_without_qualifying_evidence_is_downgraded(self):
        evidence = []
        synthesis = SynthesisResult(
            evidence_stances=[], suggested_result="false", claim_confidence=0.99, reasoning_text="r"
        )
        verdict = classify_completed_investigation("claim", "other", evidence, synthesis, HIGH_IMPACT, MIN_TIER2)
        assert verdict.result == "unverified"
        assert verdict.policy_override_applied is True

    def test_investigation_complete_is_true_and_no_incomplete_reason(self):
        evidence = [_evidence("supporting", 1)]
        synthesis = SynthesisResult(
            evidence_stances=["supporting"], suggested_result="verified", claim_confidence=0.9, reasoning_text="r"
        )
        verdict = classify_completed_investigation("claim", "other", evidence, synthesis, HIGH_IMPACT, MIN_TIER2)
        assert verdict.investigation_complete is True
        assert verdict.incomplete_reason is None


class TestClassifyIncompleteInvestigation:
    def test_always_insufficient_evidence(self):
        verdict = classify_incomplete_investigation("claim", "other", "search_limit", [])
        assert verdict.result == "insufficient_evidence"
        assert verdict.investigation_complete is False
        assert verdict.incomplete_reason == "search_limit"

    def test_confidence_and_evidence_strength_are_none_not_a_number(self):
        """decisions.md §1A: a limit is never treated as evidence — assigning
        ANY confidence number here would misleadingly imply we know something."""
        verdict = classify_incomplete_investigation("claim", "other", "provider_unavailable", [])
        assert verdict.claim_confidence is None
        assert verdict.evidence_strength is None

    def test_partial_evidence_is_preserved_but_does_not_change_the_result(self):
        partial = [_evidence("supporting", 1)]  # would normally mean "verified"
        verdict = classify_incomplete_investigation("claim", "other", "timeout", partial)
        assert verdict.result == "insufficient_evidence"  # NOT verified, despite the partial lean
        assert verdict.evidence == partial  # kept for audit/transparency

    def test_reasoning_text_explicitly_states_incomplete(self):
        verdict = classify_incomplete_investigation("claim", "other", "search_limit", [])
        text = verdict.reasoning_text.lower()
        assert "does not mean" in text or "weren't able" in text
