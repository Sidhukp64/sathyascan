"""
Phase 8 Analytics/Overview API schemas (roadmap §8.5, api-design.md's
already-designed `GET /overview`: "private, current user's own aggregated
stats only"). Every number here comes from a `GROUP BY` over the caller's
OWN `analyses`/`claims` rows — never a cross-user aggregate (that's
Explore's job, a structurally separate table/router, see
app/api/v1/routers/explore.py).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ChecksByType(BaseModel):
    text: int = 0
    image: int = 0
    url: int = 0
    audio: int = 0
    video: int = 0


class ResultsByCategory(BaseModel):
    """Buckets decisions.md §1A's ResultLabel vocabulary into the three
    groups the roadmap asks for: credible / misleading_or_false / uncertain."""

    credible: int = 0
    misleading_or_false: int = 0
    uncertain: int = 0


class CategoryCount(BaseModel):
    category: str
    count: int


class LanguageCount(BaseModel):
    language: str
    count: int


class RecentActivityItem(BaseModel):
    analysis_id: UUID
    input_type: str
    overall_result: str | None
    created_at: datetime


class OverviewResponse(BaseModel):
    total_checks: int
    checks_by_type: ChecksByType
    results_by_category: ResultsByCategory
    most_checked_categories: list[CategoryCount] = Field(default_factory=list)
    language_usage: list[LanguageCount] = Field(default_factory=list)
    recent_activity: list[RecentActivityItem] = Field(default_factory=list)
