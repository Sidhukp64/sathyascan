"""
Internal Pydantic models used throughout the text fact-checking pipeline.
These are the pipeline's own working shapes — distinct from both the DB
models (app/models/) and the Claude wire format (integrations/claude_client.py).
"""

from typing import Literal

from pydantic import BaseModel, Field

ResultLabel = Literal[
    "verified",
    "false",
    "misleading",
    "partially_true",
    "unverified",
    "opinion",
    "satire",
    "insufficient_evidence",
]

IncompleteReason = Literal[
    "search_limit",
    "tool_call_limit",
    "provider_unavailable",
    "timeout",
    "infra_error",
    "other",
]

Stance = Literal["supporting", "contradicting", "neutral"]


class ExtractedClaim(BaseModel):
    text: str
    category: str = "other"  # gov_scheme | elections | health | tech | ai_media | scam | other


class ClaimExtractionResult(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    title: str
    url: str
    domain: str
    snippet: str
    credibility_tier: str  # from source_credibility_registry, or tier_3_other default
    tier_rank: int | None  # 1/2/3, None if not tiered (low_trust/blacklisted)
    stance: Stance | None = None  # filled in by EvidenceSynthesisTool
    relevance_score: float | None = None


SuggestedResult = Literal["verified", "false", "misleading", "unverified"]


class SynthesisResult(BaseModel):
    evidence_stances: list[Stance]  # same order/length as the evidence list passed in
    suggested_result: SuggestedResult
    claim_confidence: float = Field(ge=0.0, le=1.0)
    reasoning_text: str


class ClaimVerdict(BaseModel):
    claim_text: str
    category: str
    result: ResultLabel
    investigation_complete: bool
    incomplete_reason: IncompleteReason | None = None
    claim_confidence: float | None = None
    evidence_strength: float | None = None
    evidence_tier_met: int | None = None
    reasoning_text: str
    policy_override_applied: bool = False
    evidence: list[EvidenceItem] = Field(default_factory=list)
