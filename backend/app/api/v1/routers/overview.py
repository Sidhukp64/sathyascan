"""
Phase 8 Analytics/Overview API (roadmap §8.5, api-design.md's already-
designed `GET /overview`). Reuses the existing `analyses`/`claims` tables
via SQL `GROUP BY` aggregation — no new analytics infrastructure, no
full-table loads into application memory (every query here is a bounded
aggregate or a `LIMIT`-ed recent-activity list).

**Structurally separate from Explore, by design** (api-design.md:
"/overview and /explore/* must never share a query path"): every query in
this file is scoped by `Analysis.user_id == current_user.id`; Explore's
router (app/api/v1/routers/explore.py) queries a completely different table
(`explore_claim_clusters`) that has no `user_id` column to filter by in the
first place. Neither router imports from the other.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db_session
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.user import User
from app.schemas.overview import (
    CategoryCount,
    ChecksByType,
    LanguageCount,
    OverviewResponse,
    RecentActivityItem,
    ResultsByCategory,
)

router = APIRouter(prefix="/api/v1/overview", tags=["overview"])

_CREDIBLE_RESULTS = {"verified"}
_MISLEADING_RESULTS = {"false", "misleading", "partially_true"}
_RECENT_ACTIVITY_LIMIT = 10
_TOP_CATEGORIES_LIMIT = 10


@router.get("", response_model=OverviewResponse)
async def get_overview(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OverviewResponse:
    base_filter = (Analysis.user_id == user.id, Analysis.is_deleted.is_(False))

    total_result = await session.execute(select(func.count()).select_from(Analysis).where(*base_filter))
    total_checks = total_result.scalar_one()

    type_counts_result = await session.execute(
        select(Analysis.input_type, func.count()).where(*base_filter).group_by(Analysis.input_type)
    )
    type_counts = {input_type: count for input_type, count in type_counts_result.all()}
    checks_by_type = ChecksByType(
        text=type_counts.get("text", 0),
        image=type_counts.get("image", 0),
        url=type_counts.get("url", 0),
        audio=type_counts.get("audio", 0),
        video=type_counts.get("video", 0),
    )

    result_counts_result = await session.execute(
        select(Analysis.overall_result, func.count()).where(*base_filter).group_by(Analysis.overall_result)
    )
    results_by_category = ResultsByCategory()
    for overall_result, count in result_counts_result.all():
        if overall_result in _CREDIBLE_RESULTS:
            results_by_category.credible += count
        elif overall_result in _MISLEADING_RESULTS:
            results_by_category.misleading_or_false += count
        else:
            results_by_category.uncertain += count

    language_result = await session.execute(
        select(Analysis.language, func.count()).where(*base_filter).group_by(Analysis.language)
    )
    language_usage = [LanguageCount(language=lang, count=count) for lang, count in language_result.all()]

    category_result = await session.execute(
        select(Claim.category, func.count())
        .join(Analysis, Claim.analysis_id == Analysis.id)
        .where(*base_filter, Claim.category.is_not(None))
        .group_by(Claim.category)
        .order_by(func.count().desc())
        .limit(_TOP_CATEGORIES_LIMIT)
    )
    most_checked_categories = [
        CategoryCount(category=category, count=count) for category, count in category_result.all()
    ]

    recent_result = await session.execute(
        select(Analysis).where(*base_filter).order_by(Analysis.created_at.desc()).limit(_RECENT_ACTIVITY_LIMIT)
    )
    recent_activity = [
        RecentActivityItem(
            analysis_id=row.id, input_type=row.input_type, overall_result=row.overall_result, created_at=row.created_at
        )
        for row in recent_result.scalars().all()
    ]

    return OverviewResponse(
        total_checks=total_checks,
        checks_by_type=checks_by_type,
        results_by_category=results_by_category,
        most_checked_categories=most_checked_categories,
        language_usage=language_usage,
        recent_activity=recent_activity,
    )
