"""
Phase 9 user-facing Appeals API (roadmap §9.2, matching decisions.md §5's
"universal appeals access" requirement — locked since the original
architecture pass, never built until now).

**Users can never directly modify a fact-check result through this API.**
Creating an appeal only ever inserts a row into `appeals`; nothing here
writes to `analyses`/`claims`. See app/models/appeal.py's docstring.

Ownership checks follow the exact same 404-not-403 pattern as every other
per-user resource in this codebase (app/api/v1/routers/history.py,
scheduled_checks.py, analysis_sessions.py): requesting another user's
appeal_id returns 404, never 403 — a 403 would confirm the row exists and
belongs to someone else.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db_session
from app.models.analysis import Analysis
from app.models.appeal import Appeal
from app.models.claim import Claim
from app.models.user import User
from app.schemas.appeal import (
    AppealCancelResponse,
    AppealCreateRequest,
    AppealListResponse,
    AppealResponse,
)

router = APIRouter(prefix="/api/v1/appeals", tags=["appeals"])

_MAX_PAGE_SIZE = 100
# An appeal already in one of these states doesn't need — and, for
# "under_review"/terminal ones, shouldn't get — a second appeal opened
# against the exact same analysis/claim while it's still pending review.
_OPEN_APPEAL_STATUSES = ("open", "under_review", "escalated")


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


@router.post("", response_model=AppealResponse, status_code=status.HTTP_201_CREATED)
async def create_appeal(
    body: AppealCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AppealResponse:
    analysis_result = await session.execute(
        select(Analysis).where(
            Analysis.id == body.analysis_id, Analysis.user_id == user.id, Analysis.is_deleted.is_(False)
        )
    )
    analysis = analysis_result.scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")

    if body.claim_id is not None:
        claim_result = await session.execute(
            select(Claim).where(Claim.id == body.claim_id, Claim.analysis_id == analysis.id)
        )
        if claim_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found on this analysis.")

    duplicate_filters = [
        Appeal.user_id == user.id,
        Appeal.analysis_id == body.analysis_id,
        Appeal.status.in_(_OPEN_APPEAL_STATUSES),
    ]
    if body.claim_id is not None:
        duplicate_filters.append(Appeal.claim_id == body.claim_id)
    duplicate_result = await session.execute(select(Appeal.id).where(*duplicate_filters))
    if duplicate_result.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An appeal for this analysis is already open or under review.",
        )

    appeal = Appeal(
        user_id=user.id,
        analysis_id=body.analysis_id,
        claim_id=body.claim_id,
        reason_text=body.reason_text,
        status="open",
    )
    session.add(appeal)
    await session.commit()
    await session.refresh(appeal)

    return _to_response(appeal)


@router.get("", response_model=AppealListResponse)
async def list_appeals(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AppealListResponse:
    count_result = await session.execute(select(func.count()).select_from(Appeal).where(Appeal.user_id == user.id))
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(Appeal)
        .where(Appeal.user_id == user.id)
        .order_by(Appeal.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_to_response(row) for row in rows_result.scalars().all()]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return AppealListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


async def _get_owned_appeal(session: AsyncSession, user: User, appeal_id: UUID) -> Appeal:
    result = await session.execute(select(Appeal).where(Appeal.id == appeal_id, Appeal.user_id == user.id))
    appeal = result.scalar_one_or_none()
    if appeal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appeal not found.")
    return appeal


@router.get("/{appeal_id}", response_model=AppealResponse)
async def get_appeal(
    appeal_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AppealResponse:
    appeal = await _get_owned_appeal(session, user, appeal_id)
    return _to_response(appeal)


@router.post("/{appeal_id}/cancel", response_model=AppealCancelResponse)
async def cancel_appeal(
    appeal_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AppealCancelResponse:
    appeal = await _get_owned_appeal(session, user, appeal_id)
    if appeal.status != "open":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only an appeal still in 'open' status can be cancelled (current status: {appeal.status}).",
        )

    appeal.status = "cancelled"
    appeal.updated_at = datetime.now(timezone.utc)
    await session.commit()

    return AppealCancelResponse(message="Appeal cancelled.", status=appeal.status)
