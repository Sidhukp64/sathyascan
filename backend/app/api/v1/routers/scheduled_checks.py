"""
Phase 8 "Check This Tomorrow" API (decisions.md §14 — backend scheduling/
re-check infrastructure; see app/agent/scheduled_check_runner.py for
execution and app/agent/notifications.py for the still-blocked WhatsApp
send). Routes: create, list, get, cancel, retry.

Every query is scoped by `ScheduledCheck.user_id == current_user.id`, same
IDOR-safe pattern as History — an unowned `id` returns 404, never 403.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.api.v1.deps import get_current_user, get_db_session
from app.core.config import Settings, get_settings
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.scheduled_check import ScheduledCheck
from app.models.user import User
from app.schemas.scheduled_check import (
    ScheduledCheckCreateRequest,
    ScheduledCheckListResponse,
    ScheduledCheckResponse,
)

router = APIRouter(prefix="/api/v1/scheduled-checks", tags=["scheduled-checks"])

_MAX_PAGE_SIZE = 100
_ACTIVE_STATUSES = ("pending", "running")


def _to_response(check: ScheduledCheck) -> ScheduledCheckResponse:
    return ScheduledCheckResponse(
        id=check.id,
        status=check.status,
        claim_text_snapshot=check.claim_text_snapshot,
        category=check.category,
        language=check.language,
        scheduled_for=check.scheduled_for,
        executed_at=check.executed_at,
        previous_result_snapshot=check.previous_result_snapshot,
        new_result_snapshot=check.new_result_snapshot,
        credibility_changed=check.credibility_changed,
        attempts=check.attempts,
        max_attempts=check.max_attempts,
        error_message=check.error_message,
        notification_status=check.notification_status,
        created_at=check.created_at,
    )


@router.post("", response_model=ScheduledCheckResponse, status_code=status.HTTP_201_CREATED)
async def create_scheduled_check(
    payload: ScheduledCheckCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ScheduledCheckResponse:
    analysis_result = await session.execute(
        select(Analysis).where(
            Analysis.id == payload.source_analysis_id,
            Analysis.user_id == user.id,
            Analysis.is_deleted.is_(False),
        )
    )
    analysis = analysis_result.scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")

    claim_result = await session.execute(
        select(Claim).where(Claim.analysis_id == analysis.id).order_by(Claim.claim_order).limit(1)
    )
    claim = claim_result.scalar_one_or_none()
    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This analysis has no claim to schedule a re-check for.",
        )

    # Duplicate-scheduling prevention (roadmap requirement #8).
    duplicate_result = await session.execute(
        select(ScheduledCheck).where(
            ScheduledCheck.user_id == user.id,
            ScheduledCheck.source_analysis_id == analysis.id,
            ScheduledCheck.status.in_(_ACTIVE_STATUSES),
        )
    )
    if duplicate_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A re-check is already scheduled or running for this analysis.",
        )

    scheduled_for = payload.scheduled_for or (
        datetime.now(timezone.utc) + timedelta(hours=settings.scheduled_check_default_delay_hours)
    )

    check = ScheduledCheck(
        user_id=user.id,
        source_analysis_id=analysis.id,
        claim_text_snapshot=claim.claim_text,
        category=claim.category,
        language=analysis.language,
        scheduled_for=scheduled_for,
        previous_result_snapshot={
            "result": claim.result,
            "reasoning_text": claim.reasoning_text,
            "claim_confidence": float(claim.claim_confidence) if claim.claim_confidence is not None else None,
            "evidence_strength": float(claim.evidence_strength) if claim.evidence_strength is not None else None,
            "evidence_tier_met": claim.evidence_tier_met,
            "investigation_complete": claim.investigation_complete,
            "incomplete_reason": claim.incomplete_reason,
        },
    )
    session.add(check)
    await session.flush()

    await write_audit_log(
        session,
        actor_type="user",
        action="scheduled_check_created",
        entity_type="scheduled_check",
        entity_id=check.id,
    )
    await session.commit()

    return _to_response(check)


@router.get("", response_model=ScheduledCheckListResponse)
async def list_scheduled_checks(
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ScheduledCheckListResponse:
    filters = [ScheduledCheck.user_id == user.id]
    if status_filter is not None:
        filters.append(ScheduledCheck.status == status_filter)

    count_result = await session.execute(select(func.count()).select_from(ScheduledCheck).where(*filters))
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(ScheduledCheck)
        .where(*filters)
        .order_by(ScheduledCheck.scheduled_for.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = rows_result.scalars().all()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return ScheduledCheckListResponse(
        items=[_to_response(row) for row in rows], page=page, page_size=page_size, total=total, total_pages=total_pages
    )


async def _get_owned_check(session: AsyncSession, user: User, check_id: UUID) -> ScheduledCheck:
    result = await session.execute(
        select(ScheduledCheck).where(ScheduledCheck.id == check_id, ScheduledCheck.user_id == user.id)
    )
    check = result.scalar_one_or_none()
    if check is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled check not found.")
    return check


@router.get("/{check_id}", response_model=ScheduledCheckResponse)
async def get_scheduled_check(
    check_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ScheduledCheckResponse:
    check = await _get_owned_check(session, user, check_id)
    return _to_response(check)


@router.delete("/{check_id}", response_model=ScheduledCheckResponse)
async def cancel_scheduled_check(
    check_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ScheduledCheckResponse:
    check = await _get_owned_check(session, user, check_id)
    if check.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel a scheduled check with status '{check.status}'.",
        )
    check.status = "cancelled"

    await write_audit_log(
        session, actor_type="user", action="scheduled_check_cancelled", entity_type="scheduled_check", entity_id=check.id
    )
    await session.commit()
    return _to_response(check)


@router.post("/{check_id}/retry", response_model=ScheduledCheckResponse)
async def retry_scheduled_check(
    check_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ScheduledCheckResponse:
    check = await _get_owned_check(session, user, check_id)
    if check.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot retry a scheduled check with status '{check.status}'.",
        )
    check.status = "pending"
    check.attempts = 0
    check.error_message = None
    check.scheduled_for = datetime.now(timezone.utc)  # re-run on the next tick, not re-queued for "tomorrow" again

    await write_audit_log(
        session, actor_type="user", action="scheduled_check_retried", entity_type="scheduled_check", entity_id=check.id
    )
    await session.commit()
    return _to_response(check)
