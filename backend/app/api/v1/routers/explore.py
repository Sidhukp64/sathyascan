"""
Phase 8 Explore API (docs/api-design.md: "Explore — PUBLIC, rate-limited,
no auth required"). Reads ONLY from `explore_claim_clusters`
(app/models/explore_claim_cluster.py) — a table with no FK to
`users`/`analyses` at all, populated by a periodic rollup
(app/agent/explore_rollup.py), never a live join over per-user data. This
is docs/api-design.md's "Structural privacy enforcement" made literal: this
router's repository query CANNOT join to `users` even by accident, because
there is no column here to join on.

No `Depends(get_current_user)` anywhere in this file — deliberately, per
the locked design. Rate limiting is per-IP (api-design.md: "per-user for
authenticated routes, per-IP for public ones"), reusing the same
`RateLimiter` class every other rate limit in this codebase uses.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_db_session, get_redis
from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimiter
from app.models.explore_claim_cluster import ExploreClaimCluster
from app.schemas.explore import (
    ExploreCategoriesResponse,
    ExploreCategoryCount,
    ExploreClaimListResponse,
    ExploreClaimResponse,
)

router = APIRouter(prefix="/api/v1/explore", tags=["explore"])

_MAX_PAGE_SIZE = 100
_SORT_OPTIONS = {"trending", "recent"}


def _to_response(cluster: ExploreClaimCluster) -> ExploreClaimResponse:
    return ExploreClaimResponse(
        id=cluster.id,
        representative_claim_text=cluster.representative_claim_text,
        category=cluster.category,
        language=cluster.language,
        credibility_status=cluster.credibility_status,
        credibility_score=float(cluster.credibility_score) if cluster.credibility_score is not None else None,
        source_count=cluster.source_count,
        check_count=cluster.check_count,
        first_seen_at=cluster.first_seen_at,
        last_seen_at=cluster.last_seen_at,
    )


async def _enforce_public_rate_limit(request: Request, redis: Redis, settings: Settings) -> None:
    client_ip = request.client.host if request.client else "unknown"
    limiter = RateLimiter(redis, settings.explore_rate_limit_per_ip_per_minute)
    result = await limiter.check_and_increment(f"explore-ip:{client_ip}")
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests. Please slow down."
        )


@router.get("/claims", response_model=ExploreClaimListResponse)
async def list_explore_claims(
    request: Request,
    sort: str = Query(default="trending"),
    category: str | None = Query(default=None),
    credibility_status: str | None = Query(default=None),
    language: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=500),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ExploreClaimListResponse:
    await _enforce_public_rate_limit(request, redis, settings)

    if sort not in _SORT_OPTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"sort must be one of: {sorted(_SORT_OPTIONS)}")

    filters = []
    if category is not None:
        filters.append(ExploreClaimCluster.category == category)
    if credibility_status is not None:
        filters.append(ExploreClaimCluster.credibility_status == credibility_status)
    if language is not None:
        filters.append(ExploreClaimCluster.language == language)
    if q:
        filters.append(ExploreClaimCluster.representative_claim_text.ilike(f"%{q}%"))

    count_result = await session.execute(select(func.count()).select_from(ExploreClaimCluster).where(*filters))
    total = count_result.scalar_one()

    order_by = (
        ExploreClaimCluster.check_count.desc()
        if sort == "trending"
        else ExploreClaimCluster.last_seen_at.desc()
    )
    rows_result = await session.execute(
        select(ExploreClaimCluster)
        .where(*filters)
        .order_by(order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = rows_result.scalars().all()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return ExploreClaimListResponse(
        items=[_to_response(row) for row in rows], page=page, page_size=page_size, total=total, total_pages=total_pages
    )


@router.get("/categories", response_model=ExploreCategoriesResponse)
async def list_explore_categories(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ExploreCategoriesResponse:
    await _enforce_public_rate_limit(request, redis, settings)

    result = await session.execute(
        select(ExploreClaimCluster.category, func.count())
        .where(ExploreClaimCluster.category.is_not(None))
        .group_by(ExploreClaimCluster.category)
        .order_by(func.count().desc())
    )
    return ExploreCategoriesResponse(
        categories=[ExploreCategoryCount(category=category, count=count) for category, count in result.all()]
    )


@router.get("/claims/{cluster_id}", response_model=ExploreClaimResponse)
async def get_explore_claim(
    cluster_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ExploreClaimResponse:
    await _enforce_public_rate_limit(request, redis, settings)

    result = await session.execute(select(ExploreClaimCluster).where(ExploreClaimCluster.id == cluster_id))
    cluster = result.scalar_one_or_none()
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return _to_response(cluster)
