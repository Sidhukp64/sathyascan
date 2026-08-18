"""Admin core API schemas (Phase 9, roadmap §9.1)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AdminUserSummary(BaseModel):
    """Deliberately never includes phone_number_hash/phone_number_encrypted
    in full — decisions.md §7's "never returned by any API response" rule
    applies here exactly as it does to every user-facing route; admins
    manage accounts by user_id, not by phone number."""

    id: UUID
    is_deleted: bool
    is_suspended: bool
    privacy_mode: bool
    preferred_language: str
    created_at: datetime
    last_active_at: datetime


class AdminUserDetail(AdminUserSummary):
    suspended_at: datetime | None
    suspended_reason: str | None
    analysis_count: int
    times_reported: int


class AdminUserListResponse(BaseModel):
    items: list[AdminUserSummary]
    page: int
    page_size: int
    total: int
    total_pages: int


class SuspendUserRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class SuspendUserResponse(BaseModel):
    message: str
    user_id: UUID
    is_suspended: bool


class UserStatsResponse(BaseModel):
    total_users: int
    active_last_30_days: int
    privacy_mode_on: int
    privacy_mode_off: int
    suspended: int
    deleted: int


class AnalysisStatsResponse(BaseModel):
    total_analyses: int
    by_input_type: dict[str, int]
    by_status: dict[str, int]
    by_overall_result: dict[str, int]


class ProviderStatusEntry(BaseModel):
    provider_key: str
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_type: str | None
    last_latency_ms: float | None
    success_count: int
    failure_count: int
    consecutive_failures: int


class SystemProvidersResponse(BaseModel):
    # Config-derived, one-time facts (same fields /health already exposes
    # publicly, repeated here for a single admin-facing view) ...
    safety_gate_provider_configured: bool
    speech_to_text_provider: str
    audio_forensics_provider: str
    video_forensics_provider: str
    # ... plus the LIVE, changing signal /health does NOT expose (roadmap
    # §9.5's "availability/failures/latency/recent errors").
    providers: list[ProviderStatusEntry]


class JobStatusEntry(BaseModel):
    job_name: str
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    run_count: int
    failure_count: int


class SystemJobsResponse(BaseModel):
    jobs: list[JobStatusEntry]


class SystemHealthResponse(BaseModel):
    status: str
    db: bool
    redis: bool
    safety_gate_provider_configured: bool
    jwt_configured: bool


class AuditLogEntryResponse(BaseModel):
    id: UUID
    actor_type: str
    action: str
    entity_type: str | None
    entity_id: UUID | None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: list[AuditLogEntryResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
