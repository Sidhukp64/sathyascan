"""
Phase 8 Conversation/Analysis Session API (roadmap §8.6) — grouping related
History items. Routed at `/api/v1/sessions` (short, and deliberately NOT
`/api/v1/conversation-sessions`, to avoid implying this is the
already-documented, different-purpose `conversation_sessions` design — see
app/models/analysis_session.py's docstring).

Every query scoped by `AnalysisSession.user_id`/`Analysis.user_id ==
current_user.id` — same IDOR-safe 404-not-403 pattern as every other
Phase 6-8 router. Adding/removing an analysis to/from a session requires
owning BOTH rows.

Deleting a session does not delete its analyses — only ungroups them
(`session_id` set back to NULL), matching the model's `ON DELETE SET NULL`
FK design and explicitly NOT relying on that FK firing automatically (same
"SQLite in this project's tests never enforces FK actions" reasoning
app/agent/retention.py already documents) — the null-out is done explicitly
here, in the same request, before the session row is deleted.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db_session
from app.models.analysis import Analysis
from app.models.analysis_session import AnalysisSession
from app.models.user import User
from app.schemas.analysis_session import (
    AddAnalysisToSessionRequest,
    AnalysisSessionCreateRequest,
    AnalysisSessionDetailResponse,
    AnalysisSessionListResponse,
    AnalysisSessionMemberSummary,
    AnalysisSessionRenameRequest,
    AnalysisSessionResponse,
)

router = APIRouter(prefix="/api/v1/sessions", tags=["analysis-sessions"])

_MAX_PAGE_SIZE = 100


async def _get_owned_session(session: AsyncSession, user: User, session_id: UUID) -> AnalysisSession:
    result = await session.execute(
        select(AnalysisSession).where(AnalysisSession.id == session_id, AnalysisSession.user_id == user.id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return row


async def _count_members(session: AsyncSession, session_id: UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(Analysis).where(Analysis.session_id == session_id, Analysis.is_deleted.is_(False))
    )
    return result.scalar_one()


async def _to_response(session: AsyncSession, row: AnalysisSession) -> AnalysisSessionResponse:
    return AnalysisSessionResponse(
        id=row.id,
        name=row.name,
        analysis_count=await _count_members(session, row.id),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", response_model=AnalysisSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: AnalysisSessionCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisSessionResponse:
    row = AnalysisSession(user_id=user.id, name=payload.name)
    session.add(row)
    await session.commit()
    return await _to_response(session, row)


@router.get("", response_model=AnalysisSessionListResponse)
async def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisSessionListResponse:
    count_result = await session.execute(
        select(func.count()).select_from(AnalysisSession).where(AnalysisSession.user_id == user.id)
    )
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(AnalysisSession)
        .where(AnalysisSession.user_id == user.id)
        .order_by(AnalysisSession.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = rows_result.scalars().all()
    items = [await _to_response(session, row) for row in rows]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return AnalysisSessionListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


@router.get("/{session_id}", response_model=AnalysisSessionDetailResponse)
async def get_session(
    session_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisSessionDetailResponse:
    row = await _get_owned_session(session, user, session_id)
    members_result = await session.execute(
        select(Analysis)
        .where(Analysis.session_id == row.id, Analysis.is_deleted.is_(False))
        .order_by(Analysis.created_at.desc())
    )
    members = [
        AnalysisSessionMemberSummary(
            analysis_id=m.id, input_type=m.input_type, overall_result=m.overall_result, created_at=m.created_at
        )
        for m in members_result.scalars().all()
    ]
    base = await _to_response(session, row)
    return AnalysisSessionDetailResponse(**base.model_dump(), analyses=members)


@router.put("/{session_id}", response_model=AnalysisSessionResponse)
async def rename_session(
    session_id: UUID,
    payload: AnalysisSessionRenameRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisSessionResponse:
    row = await _get_owned_session(session, user, session_id)
    row.name = payload.name
    await session.commit()
    return await _to_response(session, row)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    row = await _get_owned_session(session, user, session_id)

    # Explicit ungroup BEFORE delete — see module docstring.
    await session.execute(update(Analysis).where(Analysis.session_id == row.id).values(session_id=None))
    await session.delete(row)
    await session.commit()


@router.post("/{session_id}/analyses", response_model=AnalysisSessionDetailResponse)
async def add_analysis_to_session(
    session_id: UUID,
    payload: AddAnalysisToSessionRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisSessionDetailResponse:
    row = await _get_owned_session(session, user, session_id)

    analysis_result = await session.execute(
        select(Analysis).where(
            Analysis.id == payload.analysis_id, Analysis.user_id == user.id, Analysis.is_deleted.is_(False)
        )
    )
    analysis = analysis_result.scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")

    analysis.session_id = row.id
    await session.commit()
    return await get_session(session_id=session_id, user=user, session=session)


@router.delete("/{session_id}/analyses/{analysis_id}", response_model=AnalysisSessionDetailResponse)
async def remove_analysis_from_session(
    session_id: UUID,
    analysis_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisSessionDetailResponse:
    row = await _get_owned_session(session, user, session_id)

    analysis_result = await session.execute(
        select(Analysis).where(
            Analysis.id == analysis_id, Analysis.user_id == user.id, Analysis.session_id == row.id
        )
    )
    analysis = analysis_result.scalar_one_or_none()
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found in this session.")

    analysis.session_id = None
    await session.commit()
    return await get_session(session_id=session_id, user=user, session=session)
