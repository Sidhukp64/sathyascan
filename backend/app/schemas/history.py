"""
History API response schemas (Phase 6, api-design.md's `GET /history`,
`GET /history/{id}`, `DELETE /history/{id}`, `DELETE /history`).

Reuses `app.agent.schemas.ClaimVerdict` directly for the `claims` field
rather than defining a parallel claim schema — it already matches
api-design.md's documented evidence-first response shape (result,
reasoning_text, claim_confidence, evidence_strength, evidence_tier_met,
investigation_complete, incomplete_reason, evidence[]) and is exactly what
app.agent.claim_pipeline_shared.load_verdicts already returns; duplicating
it here would be exactly the "don't duplicate fact-checking logic" mistake
this phase was explicitly told to avoid.

`claims[].media_confidence` from api-design.md's illustrative JSON is
intentionally NOT included here: nothing in this codebase populates
`claims.media_confidence` yet (see app/models/claim.py's "unused in Phase 2"
comment, still true through Phase 5) — omitted rather than always returning
null for a field with no real data behind it anywhere in the system.

`detector` (api-design.md's per-claim forensics block) is surfaced here at
the ANALYSIS level instead of per-claim: `media_forensics_results` is keyed
to a `media_attachment_id`, not a `claim_id` — every image/audio/video
analysis in this codebase produces at most one claim per media attachment in
practice, so nesting an identical detector block into each claim would only
duplicate the same data, not add information. A disclosed simplification of
api-design.md's illustrative example, not a locked-decision deviation.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.agent.schemas import ClaimVerdict


class DetectorInfo(BaseModel):
    provider_name: str
    model_version: str
    probability: float | None = None
    label: str | None = None


class HistoryItemSummary(BaseModel):
    """One row of `GET /history` — no claims/evidence (keeps the list
    endpoint cheap); full detail is a separate `GET /history/{id}` call."""

    analysis_id: UUID
    input_type: str
    status: str
    overall_result: str | None
    language: str
    budget_limit_hit: bool
    created_at: datetime
    checked_at: datetime | None  # analyses.completed_at


class HistoryListResponse(BaseModel):
    items: list[HistoryItemSummary]
    page: int
    page_size: int
    total: int
    total_pages: int


class HistoryDetailResponse(BaseModel):
    analysis_id: UUID
    input_type: str
    status: str
    overall_result: str | None
    claims: list[ClaimVerdict] = Field(default_factory=list)
    checked_at: datetime | None
    language: str
    budget_limit_hit: bool
    detectors: list[DetectorInfo] = Field(default_factory=list)


class HistoryDeleteResponse(BaseModel):
    message: str
    deleted_count: int
