"""
Explore API schemas (Phase 8, docs/api-design.md's public `/explore/*`
routes). Every field here comes directly off `explore_claim_clusters` — a
table with no FK to `users`/`analyses` at all (see
app/models/explore_claim_cluster.py) — so there is structurally nothing
private to accidentally serialize here.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ExploreClaimResponse(BaseModel):
    id: UUID
    representative_claim_text: str
    category: str | None
    language: str
    credibility_status: str
    credibility_score: float | None
    source_count: int
    check_count: int
    first_seen_at: datetime
    last_seen_at: datetime


class ExploreClaimListResponse(BaseModel):
    items: list[ExploreClaimResponse]
    page: int
    page_size: int
    total: int
    total_pages: int


class ExploreCategoryCount(BaseModel):
    category: str
    count: int


class ExploreCategoriesResponse(BaseModel):
    categories: list[ExploreCategoryCount]
