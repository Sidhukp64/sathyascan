"""
Phase 6 History API (docs/api-design.md's `GET /history?type=&result=&from=&
to=&q=&page=`, `GET /history/{id}`, `DELETE /history/{id}`, `DELETE
/history`). agent-architecture.md's History Manager contract:
`{user_id, filters} -> {analyses[]}` — implemented here directly against the
existing `analyses`/`claims`/`evidence` tables via
app.agent.claim_pipeline_shared.load_verdicts (Phase 2's own claims+evidence
assembly logic, reused verbatim) rather than a new query layer.

Every query in this module is scoped by `Analysis.user_id ==
current_user.id` — never a client-supplied user_id — per api-design.md's
"Authorization: dashboard routes are scoped to the JWT's user_id" rule.
Requesting another user's analysis_id (detail or delete) returns 404, not
403 — a 403 would confirm the row exists and belongs to someone else
(IDOR/enumeration-adjacent leak); 404 is indistinguishable from "no such
analysis at all".

**Scope note (disclosed, not silent)**: `POST /history/{id}/reopen` from
api-design.md's route list is NOT implemented. Its semantics are undefined
by any locked doc (agent-architecture.md's History Manager contract has no
"reopen" concept, and nothing in decisions.md specifies what re-running an
investigation means operationally — re-execute the whole pipeline? reset
status to pending and let it re-run passively? at what cost/budget?).
Guessing an implementation would risk exactly the "duplicate fact-checking
logic" this phase was explicitly told to avoid. Flagged in the final report,
not silently dropped.

**Confirmed conflict, resolved with the user (AskUserQuestion, Phase 6
finalization)**: database-schema.md's "Privacy Mode ↔ schema behavior" table
lists "History / Saved Reports / Scheduled Checks: Disabled" for Privacy
Mode ON (the default for every new user). This module does NOT gate History
behind `privacy_mode` — every authenticated user can list/view/delete their
own history regardless of that setting. Confirmed as the intended
interpretation: Privacy Mode's effect is delivered entirely through the
aggressive retention purge (app/agent/retention.py — media in
PRIVACY_MODE_MEDIA_TTL_MINUTES, analyses in PRIVACY_MODE_ANALYSIS_TTL_HOURS),
which already makes a privacy-mode user's history naturally near-empty
within ~48h; blocking the endpoint outright would additionally prevent a
user from seeing an analysis that completed minutes ago, for no privacy
benefit (the data returned is always scoped to its owner already).
"""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.agent.claim_pipeline_shared import load_verdicts
from app.agent.pdf_report import generate_analysis_report_pdf
from app.api.v1.deps import get_current_user, get_db_session
from app.models.analysis import Analysis
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.user import User
from app.schemas.history import (
    DetectorInfo,
    HistoryDeleteResponse,
    HistoryDetailResponse,
    HistoryItemSummary,
    HistoryListResponse,
)

router = APIRouter(prefix="/api/v1/history", tags=["dashboard-history"])

_MAX_PAGE_SIZE = 100


async def _load_detectors(session: AsyncSession, analysis_id: UUID) -> list[DetectorInfo]:
    result = await session.execute(
        select(MediaForensicsResult)
        .join(MediaAttachment, MediaForensicsResult.media_attachment_id == MediaAttachment.id)
        .where(MediaAttachment.analysis_id == analysis_id)
    )
    rows = result.scalars().all()
    return [
        DetectorInfo(
            provider_name=row.provider_name,
            model_version=row.model_version,
            probability=(
                float(row.ai_generated_probability) if row.ai_generated_probability is not None else None
            ),
            label=None,  # no raw label column exists; raw_output carries provider-specific detail, not surfaced here
        )
        for row in rows
    ]


@router.get("", response_model=HistoryListResponse)
async def list_history(
    type: str | None = Query(default=None, description="Filter by input_type (text/image/audio/video/url)."),
    result: str | None = Query(default=None, description="Filter by overall_result."),
    language: str | None = Query(default=None, description="Filter by analyses.language."),
    from_date: date | None = Query(default=None, alias="from", description="Inclusive lower bound on created_at."),
    to_date: date | None = Query(default=None, alias="to", description="Inclusive upper bound on created_at."),
    q: str | None = Query(default=None, max_length=500, description="Substring search over the user's own input_text."),
    sort: str = Query(default="newest", description="'newest' or 'oldest', by created_at."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> HistoryListResponse:
    if sort not in ("newest", "oldest"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sort must be 'newest' or 'oldest'.")

    filters = [Analysis.user_id == user.id, Analysis.is_deleted.is_(False)]
    if type is not None:
        filters.append(Analysis.input_type == type)
    if result is not None:
        filters.append(Analysis.overall_result == result)
    if language is not None:
        filters.append(Analysis.language == language)
    if from_date is not None:
        filters.append(Analysis.created_at >= datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc))
    if to_date is not None:
        # Inclusive of the whole `to` day.
        filters.append(
            Analysis.created_at
            < datetime.combine(to_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
        )
    if q:
        filters.append(Analysis.input_text.ilike(f"%{q}%"))

    count_result = await session.execute(select(func.count()).select_from(Analysis).where(*filters))
    total = count_result.scalar_one()

    order_by = Analysis.created_at.desc() if sort == "newest" else Analysis.created_at.asc()
    rows_result = await session.execute(
        select(Analysis)
        .where(*filters)
        .order_by(order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = rows_result.scalars().all()

    items = [
        HistoryItemSummary(
            analysis_id=row.id,
            input_type=row.input_type,
            status=row.status,
            overall_result=row.overall_result,
            language=row.language,
            budget_limit_hit=row.budget_limit_hit,
            created_at=row.created_at,
            checked_at=row.completed_at,
        )
        for row in rows
    ]

    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return HistoryListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


async def _get_owned_analysis(session: AsyncSession, user: User, analysis_id: UUID) -> Analysis:
    result = await session.execute(
        select(Analysis).where(
            Analysis.id == analysis_id, Analysis.user_id == user.id, Analysis.is_deleted.is_(False)
        )
    )
    analysis = result.scalar_one_or_none()
    if analysis is None:
        # Deliberately identical to "doesn't exist" — see module docstring.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")
    return analysis


@router.get("/{analysis_id}", response_model=HistoryDetailResponse)
async def get_history_item(
    analysis_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> HistoryDetailResponse:
    analysis = await _get_owned_analysis(session, user, analysis_id)
    claims = await load_verdicts(session, analysis.id)
    detectors = await _load_detectors(session, analysis.id)

    return HistoryDetailResponse(
        analysis_id=analysis.id,
        input_type=analysis.input_type,
        status=analysis.status,
        overall_result=analysis.overall_result,
        claims=claims,
        checked_at=analysis.completed_at,
        language=analysis.language,
        budget_limit_hit=analysis.budget_limit_hit,
        detectors=detectors,
    )


@router.get("/{analysis_id}/report.pdf")
async def download_analysis_report_pdf(
    analysis_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Phase 8 PDF report (roadmap §8.2). Reuses `_get_owned_analysis` —
    same 404-not-403 IDOR-safe ownership check as every other History
    route — so a report can only ever be generated for the caller's own
    analysis. Generated on demand and streamed directly; never written to
    disk or any blob store (see app/agent/pdf_report.py's docstring)."""
    analysis = await _get_owned_analysis(session, user, analysis_id)
    pdf_bytes = await generate_analysis_report_pdf(session, analysis)

    await write_audit_log(
        session,
        actor_type="user",
        action="report_downloaded",
        entity_type="analysis",
        entity_id=analysis.id,
    )
    await session.commit()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="sathyascan-report-{analysis.id}.pdf"'},
    )


@router.delete("/{analysis_id}", response_model=HistoryDeleteResponse)
async def delete_history_item(
    analysis_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> HistoryDeleteResponse:
    analysis = await _get_owned_analysis(session, user, analysis_id)
    analysis.is_deleted = True
    analysis.deleted_at = datetime.now(timezone.utc)

    await write_audit_log(
        session,
        actor_type="user",
        action="history_item_deleted",
        entity_type="analysis",
        entity_id=analysis.id,
    )
    await session.commit()

    return HistoryDeleteResponse(message="Deleted.", deleted_count=1)


@router.delete("", response_model=HistoryDeleteResponse)
async def clear_history(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> HistoryDeleteResponse:
    result = await session.execute(
        select(Analysis).where(Analysis.user_id == user.id, Analysis.is_deleted.is_(False))
    )
    rows = result.scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.is_deleted = True
        row.deleted_at = now

    await write_audit_log(
        session,
        actor_type="user",
        action="history_cleared",
        entity_type="user",
        entity_id=user.id,
        metadata={"deleted_count": len(rows)},
    )
    await session.commit()

    return HistoryDeleteResponse(message="History cleared.", deleted_count=len(rows))
