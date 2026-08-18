"""
Phase 9 admin Moderation review (roadmap §9.3). Behind
`require_moderator_or_admin_role`, same rationale as admin_appeals.py.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.api.v1.deps import get_db_session, require_moderator_or_admin_role
from app.models.admin_user import AdminUser
from app.models.moderation_report import ModerationReport
from app.schemas.moderation import (
    ADMIN_SETTABLE_STATUSES,
    AdminModerationReviewRequest,
    ModerationReportListResponse,
    ModerationReportResponse,
)

router = APIRouter(prefix="/api/v1/admin/moderation", tags=["admin-moderation"])

_MAX_PAGE_SIZE = 100

# "open" is the only non-terminal state — once reviewed/actioned/dismissed,
# a report stays there (a genuinely different decision, if ever needed, is
# a deliberate follow-up, not a route this endpoint exposes).
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"reviewed", "actioned", "dismissed"},
    "reviewed": {"actioned", "dismissed"},
    "actioned": set(),
    "dismissed": set(),
}


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


@router.get("/reports", response_model=ModerationReportListResponse)
async def list_all_reports(
    status_filter: str | None = Query(default=None, alias="status"),
    report_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    admin: AdminUser = Depends(require_moderator_or_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> ModerationReportListResponse:
    filters = []
    if status_filter is not None:
        filters.append(ModerationReport.status == status_filter)
    if report_type is not None:
        filters.append(ModerationReport.report_type == report_type)

    count_result = await session.execute(select(func.count()).select_from(ModerationReport).where(*filters))
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(ModerationReport)
        .where(*filters)
        .order_by(ModerationReport.created_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_to_response(row) for row in rows_result.scalars().all()]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return ModerationReportListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


async def _get_report_or_404(session: AsyncSession, report_id: UUID) -> ModerationReport:
    result = await session.execute(select(ModerationReport).where(ModerationReport.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found.")
    return report


@router.get("/reports/{report_id}", response_model=ModerationReportResponse)
async def get_report_admin(
    report_id: UUID,
    admin: AdminUser = Depends(require_moderator_or_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> ModerationReportResponse:
    report = await _get_report_or_404(session, report_id)
    return _to_response(report)


@router.post("/reports/{report_id}/review", response_model=ModerationReportResponse)
async def review_report(
    report_id: UUID,
    body: AdminModerationReviewRequest,
    admin: AdminUser = Depends(require_moderator_or_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> ModerationReportResponse:
    if body.status not in ADMIN_SETTABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"status must be one of {sorted(ADMIN_SETTABLE_STATUSES)}."
        )

    report = await _get_report_or_404(session, report_id)
    allowed = _ALLOWED_TRANSITIONS.get(report.status, set())
    if body.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot transition a report from '{report.status}' to '{body.status}'.",
        )

    previous_status = report.status
    report.status = body.status
    report.admin_notes = body.admin_notes
    report.resolved_by = admin.id
    if body.status in ("actioned", "dismissed"):
        report.resolved_at = datetime.now(timezone.utc)

    await write_audit_log(
        session,
        actor_type="admin",
        action="moderation_report_reviewed",
        entity_type="moderation_report",
        entity_id=report.id,
        metadata={"admin_id": str(admin.id), "from_status": previous_status, "to_status": body.status},
    )
    await session.commit()

    return _to_response(report)
