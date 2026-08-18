"""Moderation report schemas (Phase 9, roadmap §9.3)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

VALID_REPORT_TYPES = {"spam", "abuse", "suspicious_content", "malicious_url", "other"}
VALID_TARGET_TYPES = {"analysis", "url", "explore_claim", "other"}
ADMIN_SETTABLE_STATUSES = {"reviewed", "actioned", "dismissed"}


class ModerationReportCreateRequest(BaseModel):
    report_type: str = Field(description=f"One of: {sorted(VALID_REPORT_TYPES)}")
    target_type: str = Field(description=f"One of: {sorted(VALID_TARGET_TYPES)}")
    target_analysis_id: UUID | None = None
    target_explore_cluster_id: UUID | None = None
    target_url: str | None = Field(default=None, max_length=2000)
    description: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _exactly_one_target_reference_for_the_declared_type(self) -> "ModerationReportCreateRequest":
        # Exactly one target reference must match target_type — validated
        # here (schema layer), not as a DB constraint — see
        # app/models/moderation_report.py's docstring for why.
        if self.target_type == "analysis" and self.target_analysis_id is None:
            raise ValueError("target_analysis_id is required when target_type is 'analysis'.")
        if self.target_type == "explore_claim" and self.target_explore_cluster_id is None:
            raise ValueError("target_explore_cluster_id is required when target_type is 'explore_claim'.")
        if self.target_type == "url" and not self.target_url:
            raise ValueError("target_url is required when target_type is 'url'.")
        return self


class ModerationReportResponse(BaseModel):
    id: UUID
    report_type: str
    target_type: str
    target_analysis_id: UUID | None
    target_explore_cluster_id: UUID | None
    target_url: str | None
    description: str
    status: str
    admin_notes: str | None
    created_at: datetime
    resolved_at: datetime | None


class ModerationReportListResponse(BaseModel):
    items: list[ModerationReportResponse]
    page: int
    page_size: int
    total: int
    total_pages: int


class AdminModerationReviewRequest(BaseModel):
    status: str = Field(description=f"One of: {sorted(ADMIN_SETTABLE_STATUSES)}")
    admin_notes: str | None = Field(default=None, max_length=2000)
