"""
Phase 9 admin core API (roadmap §9.1: user management, stats, system
health/jobs/providers, audit-log viewing). Every route here requires
`require_admin_role` (role == "admin" exactly) — appeals/moderation review
(the routes moderators also need) live in separate router files
(`admin_appeals.py`/`admin_moderation.py`) behind the wider
`require_moderator_or_admin_role` gate instead, so the RBAC boundary is
enforced per-route-group, not by a single shared dependency that would make
"admin-only vs admin-or-moderator" a runtime `if` check instead of a
structural one.

Reuses existing infrastructure throughout, per the user's explicit
instruction: `write_audit_log` (Phase 6) for every state change,
`app.core.provider_health`/`app.core.job_status` (Phase 9, this same round)
for live system signals, the same `GROUP BY`-aggregate pattern
`app/api/v1/routers/overview.py` already established for stats.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sql_text
from datetime import datetime, timedelta, timezone

from app.agent.audit import write_audit_log
from app.agent.tools.audio_forensics import NullAudioForensicsProvider
from app.agent.tools.speech_to_text import NullSpeechToTextProvider
from app.agent.tools.video_forensics import NullVideoForensicsProvider
from app.api.v1.deps import get_db_session, require_admin_role
from app.core.config import get_settings
from app.core.job_status import job_status
from app.core.provider_health import provider_health
from app.models.admin_user import AdminUser
from app.models.analysis import Analysis
from app.models.moderation_report import ModerationReport
from app.models.user import User
from app.schemas.admin import (
    AdminUserDetail,
    AdminUserListResponse,
    AdminUserSummary,
    AnalysisStatsResponse,
    AuditLogEntryResponse,
    AuditLogListResponse,
    JobStatusEntry as JobStatusEntrySchema,
    ProviderStatusEntry,
    SuspendUserRequest,
    SuspendUserResponse,
    SystemHealthResponse,
    SystemJobsResponse,
    SystemProvidersResponse,
    UserStatsResponse,
)
from app.models.audit_log import AuditLog

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_MAX_PAGE_SIZE = 100


def _to_summary(user: User) -> AdminUserSummary:
    return AdminUserSummary(
        id=user.id,
        is_deleted=user.is_deleted,
        is_suspended=user.is_suspended,
        privacy_mode=user.privacy_mode,
        preferred_language=user.preferred_language,
        created_at=user.created_at,
        last_active_at=user.last_active_at,
    )


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    is_suspended: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserListResponse:
    filters = []
    if is_suspended is not None:
        filters.append(User.is_suspended == is_suspended)

    count_result = await session.execute(select(func.count()).select_from(User).where(*filters))
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_to_summary(row) for row in rows_result.scalars().all()]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return AdminUserListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


async def _get_user_or_404(session: AsyncSession, user_id: UUID) -> User:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def get_user_detail(
    user_id: UUID,
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserDetail:
    user = await _get_user_or_404(session, user_id)

    analysis_count_result = await session.execute(
        select(func.count()).select_from(Analysis).where(Analysis.user_id == user.id)
    )
    analysis_count = analysis_count_result.scalar_one()

    # "Repeated abuse" (roadmap §9.3) — how many moderation reports target
    # ANY analysis this user owns. A subquery, not a stored counter, so it's
    # always live-accurate even as reports/analyses come and go.
    times_reported_result = await session.execute(
        select(func.count())
        .select_from(ModerationReport)
        .where(
            ModerationReport.target_analysis_id.in_(
                select(Analysis.id).where(Analysis.user_id == user.id)
            )
        )
    )
    times_reported = times_reported_result.scalar_one()

    return AdminUserDetail(
        **_to_summary(user).model_dump(),
        suspended_at=user.suspended_at,
        suspended_reason=user.suspended_reason,
        analysis_count=analysis_count,
        times_reported=times_reported,
    )


@router.post("/users/{user_id}/suspend", response_model=SuspendUserResponse)
async def suspend_user(
    user_id: UUID,
    body: SuspendUserRequest,
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> SuspendUserResponse:
    user = await _get_user_or_404(session, user_id)
    if user.is_suspended:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is already suspended.")

    user.is_suspended = True
    user.suspended_at = datetime.now(timezone.utc)
    user.suspended_reason = body.reason

    await write_audit_log(
        session,
        actor_type="admin",
        action="user_suspended",
        entity_type="user",
        entity_id=user.id,
        metadata={"admin_id": str(admin.id)},
    )
    await session.commit()

    return SuspendUserResponse(message="User suspended.", user_id=user.id, is_suspended=True)


@router.post("/users/{user_id}/reactivate", response_model=SuspendUserResponse)
async def reactivate_user(
    user_id: UUID,
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> SuspendUserResponse:
    user = await _get_user_or_404(session, user_id)
    if not user.is_suspended:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not currently suspended.")

    user.is_suspended = False
    user.suspended_at = None
    user.suspended_reason = None

    await write_audit_log(
        session,
        actor_type="admin",
        action="user_reactivated",
        entity_type="user",
        entity_id=user.id,
        metadata={"admin_id": str(admin.id)},
    )
    await session.commit()

    return SuspendUserResponse(message="User reactivated.", user_id=user.id, is_suspended=False)


@router.get("/stats/users", response_model=UserStatsResponse)
async def user_stats(
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> UserStatsResponse:
    total_result = await session.execute(select(func.count()).select_from(User))
    total_users = total_result.scalar_one()

    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    active_result = await session.execute(
        select(func.count()).select_from(User).where(User.last_active_at >= thirty_days_ago)
    )
    active_last_30_days = active_result.scalar_one()

    privacy_on_result = await session.execute(
        select(func.count()).select_from(User).where(User.privacy_mode.is_(True))
    )
    privacy_mode_on = privacy_on_result.scalar_one()

    privacy_off_result = await session.execute(
        select(func.count()).select_from(User).where(User.privacy_mode.is_(False))
    )
    privacy_mode_off = privacy_off_result.scalar_one()

    suspended_result = await session.execute(
        select(func.count()).select_from(User).where(User.is_suspended.is_(True))
    )
    suspended = suspended_result.scalar_one()

    deleted_result = await session.execute(select(func.count()).select_from(User).where(User.is_deleted.is_(True)))
    deleted = deleted_result.scalar_one()

    return UserStatsResponse(
        total_users=total_users,
        active_last_30_days=active_last_30_days,
        privacy_mode_on=privacy_mode_on,
        privacy_mode_off=privacy_mode_off,
        suspended=suspended,
        deleted=deleted,
    )


@router.get("/stats/analyses", response_model=AnalysisStatsResponse)
async def analysis_stats(
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AnalysisStatsResponse:
    total_result = await session.execute(select(func.count()).select_from(Analysis))
    total_analyses = total_result.scalar_one()

    type_result = await session.execute(select(Analysis.input_type, func.count()).group_by(Analysis.input_type))
    by_input_type = {row[0]: row[1] for row in type_result.all()}

    status_result = await session.execute(select(Analysis.status, func.count()).group_by(Analysis.status))
    by_status = {row[0]: row[1] for row in status_result.all()}

    result_result = await session.execute(
        select(Analysis.overall_result, func.count()).group_by(Analysis.overall_result)
    )
    by_overall_result = {(row[0] or "none"): row[1] for row in result_result.all()}

    return AnalysisStatsResponse(
        total_analyses=total_analyses, by_input_type=by_input_type, by_status=by_status, by_overall_result=by_overall_result
    )


@router.get("/system/health", response_model=SystemHealthResponse)
async def system_health(
    request: Request,
    admin: AdminUser = Depends(require_admin_role),
) -> SystemHealthResponse:
    db_ok = True
    try:
        async with request.app.state.db_engine.connect() as conn:
            await conn.execute(sql_text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health check must never raise
        db_ok = False

    redis_ok = True
    try:
        await request.app.state.redis.ping()
    except Exception:  # noqa: BLE001
        redis_ok = False

    settings = get_settings()
    return SystemHealthResponse(
        status="ok" if (db_ok and redis_ok) else "degraded",
        db=db_ok,
        redis=redis_ok,
        safety_gate_provider_configured=bool(settings.safety_gate_provider),
        jwt_configured=bool(settings.jwt_secret),
    )


@router.get("/system/jobs", response_model=SystemJobsResponse)
async def system_jobs(admin: AdminUser = Depends(require_admin_role)) -> SystemJobsResponse:
    snapshot = job_status.snapshot()
    jobs = [
        JobStatusEntrySchema(
            job_name=entry.job_name,
            last_run_at=entry.last_run_at,
            last_success_at=entry.last_success_at,
            last_error=entry.last_error,
            run_count=entry.run_count,
            failure_count=entry.failure_count,
        )
        for entry in snapshot.values()
    ]
    return SystemJobsResponse(jobs=jobs)


@router.get("/system/providers", response_model=SystemProvidersResponse)
async def system_providers(request: Request, admin: AdminUser = Depends(require_admin_role)) -> SystemProvidersResponse:
    settings = get_settings()
    snapshot = provider_health.snapshot()
    providers = [
        ProviderStatusEntry(
            provider_key=entry.provider_key,
            last_success_at=entry.last_success_at,
            last_failure_at=entry.last_failure_at,
            last_error_type=entry.last_error_type,
            last_latency_ms=entry.last_latency_ms,
            success_count=entry.success_count,
            failure_count=entry.failure_count,
            consecutive_failures=entry.consecutive_failures,
        )
        for entry in snapshot.values()
    ]
    # Same isinstance-against-the-concrete-Null-class check /health's own
    # route uses (app/main.py) — introspected from the live app.state
    # object, not re-derived from config, and reusing the exact same
    # "null"/"real" convention rather than inventing a second one.
    return SystemProvidersResponse(
        safety_gate_provider_configured=bool(settings.safety_gate_provider),
        speech_to_text_provider="null" if isinstance(request.app.state.stt_provider, NullSpeechToTextProvider) else "real",
        audio_forensics_provider="null"
        if isinstance(request.app.state.audio_forensics_provider, NullAudioForensicsProvider)
        else "real",
        video_forensics_provider="null"
        if isinstance(request.app.state.video_forensics_provider, NullVideoForensicsProvider)
        else "real",
        providers=providers,
    )


@router.get("/audit-log", response_model=AuditLogListResponse)
async def list_audit_log(
    actor_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
    admin: AdminUser = Depends(require_admin_role),
    session: AsyncSession = Depends(get_db_session),
) -> AuditLogListResponse:
    """Roadmap §9.4: "Protect audit logs from ordinary users." No regular
    user or moderator route can reach this — require_admin_role only."""
    filters = []
    if actor_type is not None:
        filters.append(AuditLog.actor_type == actor_type)
    if action is not None:
        filters.append(AuditLog.action == action)

    count_result = await session.execute(select(func.count()).select_from(AuditLog).where(*filters))
    total = count_result.scalar_one()

    rows_result = await session.execute(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        AuditLogEntryResponse(
            id=row.id,
            actor_type=row.actor_type,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            created_at=row.created_at,
        )
        for row in rows_result.scalars().all()
    ]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return AuditLogListResponse(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)
