"""
In-process provider health tracking (Phase 9, roadmap §9.5: "Provider
Monitoring" — "Track where technically possible: availability, failures,
latency, recent errors, job failures"). A lightweight, in-memory registry,
NOT a new metrics/observability stack — reuses the exact try/except choke
point every Tool class in `app/agent/tools/*.py` already has (each one
already catches its provider's exceptions uniformly and converts them to a
safe status; this module just also records that the call happened).

**Deliberately distinct from `/health`'s existing `*_provider_configured`
flags** — those answer "is a real, credentialed provider class selected at
all" (a one-time, config-derived fact); this answers "is the code path that
talks to it actually succeeding right now" (a live, changing signal). A
provider can be misconfigured-off (Null stub, `/health` says so already)
without ever appearing here at all — recording a "failure" for every call
to a deliberately-unconfigured Null stub would be noise, not signal, and
would conflate two genuinely different questions. Only REAL provider
exceptions (the Tool's own `except Exception` / `except *Unavailable`
blocks) are recorded here.

**Never stores `str(exception)`** — only the exception's class name
(`type(exc).__name__`). A provider's raw error message can echo back
request/response fragments (a URL, a header, occasionally a credential in a
badly-behaved client library's error string) — the same "secrets never
logged" discipline `app/core/jwt_auth.py`/`app/integrations/*_client.py`
already apply, extended here to this new observability surface.

In-memory, per-process, reset on restart — matches this codebase's existing
"in-process asyncio periodic task, not a new external system" precedent
(retention purge, scheduled-check runner, Explore rollup) rather than
adding a metrics backend (Prometheus, etc.) nothing in this environment can
actually scrape.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone


@dataclass
class ProviderHealthEntry:
    provider_key: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_type: str | None = None
    last_latency_ms: float | None = None
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0


class ProviderHealthTracker:
    def __init__(self) -> None:
        self._entries: dict[str, ProviderHealthEntry] = {}

    def _entry(self, provider_key: str) -> ProviderHealthEntry:
        entry = self._entries.get(provider_key)
        if entry is None:
            entry = ProviderHealthEntry(provider_key=provider_key)
            self._entries[provider_key] = entry
        return entry

    def record_success(self, provider_key: str, latency_ms: float | None = None) -> None:
        entry = self._entry(provider_key)
        entry.last_success_at = datetime.now(timezone.utc)
        entry.last_latency_ms = latency_ms
        entry.success_count += 1
        entry.consecutive_failures = 0

    def record_failure(self, provider_key: str, error_type: str) -> None:
        entry = self._entry(provider_key)
        entry.last_failure_at = datetime.now(timezone.utc)
        entry.last_error_type = error_type  # class name ONLY — see module docstring
        entry.failure_count += 1
        entry.consecutive_failures += 1

    def snapshot(self) -> dict[str, ProviderHealthEntry]:
        """Returns a genuinely independent copy — both the dict AND each
        entry within it (`dataclasses.replace`, not just `dict(...)`, which
        would copy the dict but leave every value pointing at the SAME
        live entry object) — callers (the admin system-status route) must
        never be able to mutate live tracker state through what looks like
        a read-only snapshot."""
        return {key: replace(entry) for key, entry in self._entries.items()}


# Module-level singleton — same pattern as `logger = logging.getLogger(...)`
# instances scattered throughout this codebase: diagnostic/observability
# state, not business logic, so a plain shared instance (not DI-injected
# per-request) is the appropriate scope. Reset only on process restart.
provider_health = ProviderHealthTracker()
