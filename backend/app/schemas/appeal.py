"""Appeals schemas (Phase 9, roadmap §9.2)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

VALID_APPEAL_STATUSES = {"open", "under_review", "approved", "rejected", "escalated", "cancelled"}
# What an admin/moderator may transition an appeal TO via the review
# endpoint — "cancelled" is deliberately excluded (that's the OWNER's own
# action, app/api/v1/routers/appeals.py's cancel endpoint, not an admin
# decision).
ADMIN_SETTABLE_STATUSES = {"under_review", "approved", "rejected", "escalated"}


class AppealCreateRequest(BaseModel):
    analysis_id: UUID
    claim_id: UUID | None = None
    reason_text: str = Field(min_length=1, max_length=2000)


class AppealResponse(BaseModel):
    id: UUID
    analysis_id: UUID
    claim_id: UUID | None
    reason_text: str
    status: str
    admin_notes: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class AppealListResponse(BaseModel):
    items: list[AppealResponse]
    page: int
    page_size: int
    total: int
    total_pages: int


class AppealCancelResponse(BaseModel):
    message: str
    status: str


class AdminAppealReviewRequest(BaseModel):
    status: str = Field(description=f"One of: {sorted(ADMIN_SETTABLE_STATUSES)}")
    admin_notes: str | None = Field(default=None, max_length=2000)
