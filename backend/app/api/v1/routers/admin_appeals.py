"""
Phase 9 admin Appeals review (roadmap §9.2). Behind
`require_moderator_or_admin_role` — both roles may triage/review appeals
(only user-account/system-internal actions are admin-only, see
app/api/v1/routers/admin.py's module docstring).

State-machine enforcement is explicit and strict (roadmap: "invalid status
transitions" must be tested and rejected) — `_ALLOWED_TRANSITIONS` is the
single source of truth both this router and its tests check against.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.api.v1.deps import get_db_session, require_moderator_or_admin_role
from app.models.admin_user import AdminUser
from app.models.appeal import Appeal
from app.schemas.appeal import ADMIN_SETTABLE_STATUSES, AdminAppealReviewRequest, AppealListResponse, AppealResponse

router = APIRouter(prefix="/api/v1/admin/appeals", tags=["admin-appeals"])

_MAX_PAGE_SIZE = 100

# Terminal states (approved/rejected) accept no further transition through
# this endpoint — a genuinely different decision, if ever needed, is a
# deliberate follow-up action outside this table's scope (see
# app/models/appeal.py's docstring on why this table never auto-modifies a
# stored verdict).
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"under_review", "approved", "rejected", "escalated"},
    "under_review": {"approved", "rejected", "escalated"},
    "escalated": {"approved", "rejected"},
    "approved": set(),
    "rejected": set(),
    "cancelled": set(),  # a user-cancelled appeal is not admin-reviewable
}


def _to_response(appeal: Appeal) -> AppealResponse:
    return AppealResponse(
        id=appeal.id,
        analysis_id=appeal.analysis_id,
        claim_id=appeal.claim_id,
        reason_text=appeal.reason_text,
        status=appeal.status,
        admin_notes=appeal.admin_notes,
        created_at=appeal.created_at,
        updated_at=appeal.updated_at,
        resolved_at=appeal.resolved_at,
    )


@router.get("", response_model=AppealListResponse)
async def list_all_appeals(
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    admin: AdminUser = Depends(require_moderator_or_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AppealListResponse:
    filters = []
    if status_filter is not None:
        filters.append(Appeal.status == status_filter)

    count_result = await session.execute(select(func.count()).select_from(Appeal).where(*filters))
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(Appeal).where(*filters).order_by(Appeal.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
    )
    items = [_to_response(row) for row in rows_result.scalars().all()]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return AppealListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


async def _get_appeal_or_404(session: AsyncSession, appeal_id: UUID) -> Appeal:
    result = await session.execute(select(Appeal).where(Appeal.id == appeal_id))
    appeal = result.scalar_one_or_none()
    if appeal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appeal not found.")
    return appeal


@router.get("/{appeal_id}", response_model=AppealResponse)
async def get_appeal_admin(
    appeal_id: UUID,
    admin: AdminUser = Depends(require_moderator_or_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AppealResponse:
    appeal = await _get_appeal_or_404(session, appeal_id)
    return _to_response(appeal)


@router.post("/{appeal_id}/review", response_model=AppealResponse)
async def review_appeal(
    appeal_id: UUID,
    body: AdminAppealReviewRequest,
    admin: AdminUser = Depends(require_moderator_or_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AppealResponse:
    if body.status not in ADMIN_SETTABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status must be one of {sorted(ADMIN_SETTABLE_STATUSES)}.",
        )

    appeal = await _get_appeal_or_404(session, appeal_id)
    allowed = _ALLOWED_TRANSITIONS.get(appeal.status, set())
    if body.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot transition an appeal from '{appeal.status}' to '{body.status}'.",
        )

    previous_status = appeal.status
    appeal.status = body.status
    appeal.admin_notes = body.admin_notes
    appeal.resolved_by = admin.id
    appeal.updated_at = datetime.now(timezone.utc)
    if body.status in ("approved", "rejected"):
        appeal.resolved_at = datetime.now(timezone.utc)

    await write_audit_log(
        session,
        actor_type="admin",
        action="appeal_reviewed",
        entity_type="appeal",
        entity_id=appeal.id,
        metadata={"admin_id": str(admin.id), "from_status": previous_status, "to_status": body.status},
    )
    await session.commit()

    return _to_response(appeal)
