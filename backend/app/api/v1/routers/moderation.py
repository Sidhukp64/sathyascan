"""
Phase 9 user-facing Moderation reports API (roadmap §9.3). A ticketing
system for humans to review — see app/models/moderation_report.py's
docstring for why this is NOT a new AI content-moderation system.

**Reporting deliberately does NOT require owning the target** — the entire
point of "spam"/"abuse"/"suspicious content" reports is flagging SOMEONE
ELSE's content (or a public Explore item, which has no owner at all — see
explore_claim_clusters' own no-FK-to-users design). Existence is still
checked for `analysis`/`explore_claim` targets (a report against a
non-existent UUID is meaningless), just not ownership.

`GET /moderation/reports` (this router) returns only the CALLER'S OWN
submitted reports (as reporter) — never reports filed AGAINST their
content, which would let a bad actor identify and retaliate against whoever
reported them. That cross-reference only exists on the admin side
(`app/api/v1/routers/admin.py`'s `times_reported` field).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db_session
from app.models.analysis import Analysis
from app.models.explore_claim_cluster import ExploreClaimCluster
from app.models.moderation_report import ModerationReport
from app.models.user import User
from app.schemas.moderation import (
    ModerationReportCreateRequest,
    ModerationReportListResponse,
    ModerationReportResponse,
    VALID_REPORT_TYPES,
    VALID_TARGET_TYPES,
)

router = APIRouter(prefix="/api/v1/moderation", tags=["moderation"])

_MAX_PAGE_SIZE = 100


def _to_response(report: ModerationReport) -> ModerationReportResponse:
    return ModerationReportResponse(
        id=report.id,
        report_type=report.report_type,
        target_type=report.target_type,
        target_analysis_id=report.target_analysis_id,
        target_explore_cluster_id=report.target_explore_cluster_id,
        target_url=report.target_url,
        description=report.description,
        status=report.status,
        admin_notes=report.admin_notes,
        created_at=report.created_at,
        resolved_at=report.resolved_at,
    )


@router.post("/reports", response_model=ModerationReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    body: ModerationReportCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ModerationReportResponse:
    if body.report_type not in VALID_REPORT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"report_type must be one of {sorted(VALID_REPORT_TYPES)}."
        )
    if body.target_type not in VALID_TARGET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"target_type must be one of {sorted(VALID_TARGET_TYPES)}."
        )

    if body.target_type == "analysis":
        result = await session.execute(
            select(Analysis).where(Analysis.id == body.target_analysis_id, Analysis.is_deleted.is_(False))
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target analysis not found.")

    if body.target_type == "explore_claim":
        result = await session.execute(
            select(ExploreClaimCluster).where(ExploreClaimCluster.id == body.target_explore_cluster_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target Explore item not found.")

    report = ModerationReport(
        reporter_user_id=user.id,
        report_type=body.report_type,
        target_type=body.target_type,
        target_analysis_id=body.target_analysis_id,
        target_explore_cluster_id=body.target_explore_cluster_id,
        target_url=body.target_url,
        description=body.description,
        status="open",
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)

    return _to_response(report)


@router.get("/reports", response_model=ModerationReportListResponse)
async def list_own_reports(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ModerationReportListResponse:
    count_result = await session.execute(
        select(func.count()).select_from(ModerationReport).where(ModerationReport.reporter_user_id == user.id)
    )
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(ModerationReport)
        .where(ModerationReport.reporter_user_id == user.id)
        .order_by(ModerationReport.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_to_response(row) for row in rows_result.scalars().all()]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return ModerationReportListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


@router.get("/reports/{report_id}", response_model=ModerationReportResponse)
async def get_own_report(
    report_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ModerationReportResponse:
    result = await session.execute(
        select(ModerationReport).where(
            ModerationReport.id == report_id, ModerationReport.reporter_user_id == user.id
        )
    )
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found.")
    return _to_response(report)
