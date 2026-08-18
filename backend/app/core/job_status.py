"""
In-process background-job status tracking (Phase 9, roadmap §9.5: "Job
status" / §9.1's admin "Job status" requirement). Same pattern as
app/core/provider_health.py — a lightweight, in-memory, per-process
registry, not a new external system — updated by each of `main.py`'s three
existing periodic asyncio tasks (retention purge, scheduled-check runner,
Explore rollup) at every tick, exposed read-only via `GET
/api/v1/admin/system/jobs`.

Never stores a full exception message — only `type(exc).__name__`, same
"never log content that might leak internals" discipline
`provider_health.py` documents.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone


@dataclass
class JobStatusEntry:
    job_name: str
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    run_count: int = 0
    failure_count: int = 0


class JobStatusTracker:
    def __init__(self) -> None:
        self._entries: dict[str, JobStatusEntry] = {}

    def _entry(self, job_name: str) -> JobStatusEntry:
        entry = self._entries.get(job_name)
        if entry is None:
            entry = JobStatusEntry(job_name=job_name)
            self._entries[job_name] = entry
        return entry

    def record_tick_success(self, job_name: str) -> None:
        entry = self._entry(job_name)
        now = datetime.now(timezone.utc)
        entry.last_run_at = now
        entry.last_success_at = now
        entry.run_count += 1

    def record_tick_failure(self, job_name: str, error_type: str) -> None:
        entry = self._entry(job_name)
        entry.last_run_at = datetime.now(timezone.utc)
        entry.last_error = error_type
        entry.run_count += 1
        entry.failure_count += 1

    def snapshot(self) -> dict[str, JobStatusEntry]:
        """Genuinely independent copy — see
        app/core/provider_health.py's identical `snapshot()` for why a bare
        `dict(self._entries)` isn't enough (it copies the dict but leaves
        every value pointing at the same live entry object)."""
        return {key: replace(entry) for key, entry in self._entries.items()}


# Module-level singleton, same rationale as app/core/provider_health.py's.
job_status = JobStatusTracker()
