"""
Safety Gate — non-bypassable prohibited-content pre-check (decisions.md §6,
agent-architecture.md's "Safety gate" section).

Locked contract: {media_ref} -> {outcome: passed|blocked|error}. This module
builds exactly that slot — position, fail-closed behavior, and minimal event
logging — WITHOUT inventing a CSAM/illegal-content detection vendor or
reporting procedure, which decisions.md §6 explicitly reserves for mandatory
legal/professional review. `NullSafetyProvider` is a passthrough stub so the
pipeline is runnable/testable now; it performs no real content-safety check,
and every use of it is loudly logged so this can never be mistaken for real
protection in a deployment reachable by real users.

Fail-closed: unlike the rest of the pipeline (which prefers graceful
degradation to insufficient_evidence over hard failure), the safety gate
treats a provider ERROR the same as BLOCKED — it never falls through to
analysis just because the check itself failed.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class SafetyOutcome(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class SafetyCheckResult:
    outcome: SafetyOutcome
    check_type: str  # csam_hash_match | content_classifier | other
    provider_name: str | None
    reference_id: str | None = None  # vendor's own case reference, if any — NEVER the content itself


class SafetyProvider(Protocol):
    async def check(self, media_bytes: bytes, mime_type: str) -> SafetyCheckResult: ...


class NullSafetyProvider:
    """Passthrough stub. NOT a real content-safety check — decisions.md §6
    explicitly defers the vendor/procedure decision to mandatory legal
    review. Exists only so the non-bypassable slot in the pipeline is real
    and exercised end-to-end. A deployment using this provider must not be
    exposed publicly."""

    provider_name = "null_passthrough_not_a_real_check"

    async def check(self, media_bytes: bytes, mime_type: str) -> SafetyCheckResult:
        log_event(
            logger,
            logging.WARNING,
            "safety gate: no real content-safety provider configured — "
            "this deployment must not be exposed publicly (decisions.md §6)",
        )
        return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name=self.provider_name)


class SafetyGate:
    def __init__(self, provider: SafetyProvider) -> None:
        self._provider = provider

    async def check(self, media_bytes: bytes, mime_type: str) -> SafetyCheckResult:
        # Phase 9 — provider health tracking (roadmap §9.5); see
        # app/core/provider_health.py's docstring.
        started_at = time.monotonic()
        try:
            result = await self._provider.check(media_bytes, mime_type)
            provider_health.record_success("safety_gate", (time.monotonic() - started_at) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001 - any provider failure fails closed
            provider_health.record_failure("safety_gate", type(exc).__name__)
            log_event(
                logger,
                logging.ERROR,
                "safety gate provider raised — failing closed (treated as blocked)",
                provider_name=getattr(self._provider, "provider_name", "unknown"),
            )
            provider_name = getattr(self._provider, "provider_name", None)
            return SafetyCheckResult(outcome=SafetyOutcome.ERROR, check_type="other", provider_name=provider_name)

    @staticmethod
    def may_proceed(result: SafetyCheckResult) -> bool:
        """The ONLY outcome that allows analysis to continue. BLOCKED and
        ERROR are both treated as "do not proceed" — fail closed."""
        return result.outcome == SafetyOutcome.PASSED
