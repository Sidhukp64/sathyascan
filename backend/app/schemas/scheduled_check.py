"""
"Check This Tomorrow" API schemas (Phase 8, decisions.md §14).

`ScheduledCheckResponse` never includes anything beyond the snapshot
already stored on the row — no live join back to the (possibly since-
purged) source analysis, consistent with `source_analysis_id` being
nullable/SET NULL (see app/models/scheduled_check.py).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ScheduledCheckCreateRequest(BaseModel):
    source_analysis_id: UUID
    scheduled_for: datetime | None = None
    # Defaults to "tomorrow" (settings.scheduled_check_default_delay_hours)
    # server-side when omitted — see the router.


class ResultSnapshot(BaseModel):
    result: str
    reasoning_text: str
    claim_confidence: float | None = None
    evidence_strength: float | None = None
    evidence_tier_met: int | None = None
    investigation_complete: bool
    incomplete_reason: str | None = None


class ScheduledCheckResponse(BaseModel):
    id: UUID
    status: str
    claim_text_snapshot: str
    category: str | None
    language: str
    scheduled_for: datetime
    executed_at: datetime | None
    previous_result_snapshot: ResultSnapshot | None = None
    new_result_snapshot: ResultSnapshot | None = None
    credibility_changed: bool | None
    attempts: int
    max_attempts: int
    error_message: str | None
    notification_status: str
    created_at: datetime


class ScheduledCheckListResponse(BaseModel):
    items: list[ScheduledCheckResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
