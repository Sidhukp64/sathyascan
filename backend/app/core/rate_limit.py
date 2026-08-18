"""
Per-sender rate limiting foundation (decisions.md §1, §12, §16 in risks doc).

Fixed-window counter keyed by the sender's peppered phone hash, never the raw
number. This is a foundation, not the final abuse-protection design — global
concurrency caps, evidence/tool-call budgets, and duplicate-content detection
all belong to Phase 2+ once there's an AI pipeline whose cost/concurrency
those actually bound. Phase 1 only needs to stop one sender from looping the
echo endpoint.

**Phase 10 fix — a real, previously-undetected bug**: `check_and_increment`
had NO Redis-outage handling at all, across every one of its six call sites
(webhook per-sender, OTP-start per-IP, dashboard per-user, admin per-admin,
admin-login per-IP, Explore per-IP) — a Redis connection error propagated
straight into an unhandled 500. This was invisible to all 708 tests passing
at the time, because the entire test suite uses `fakeredis`, which never
simulates a connection failure; it was only caught by Phase 10's real
uvicorn boot smoke test against a genuinely unreachable Redis (see
docs/risks-and-open-questions.md's Phase 10 entry).

**Fails OPEN, not closed — a deliberate, different choice from
`app/api/v1/deps.py`'s JWT-revocation check (which fails CLOSED).** Rate
limiting exists purely for abuse/cost protection, not security-critical
access control — the revocation check's fail-closed posture protects
against a REVOKED token still working; there is no equivalent security
property being protected here. Taking every route down because the rate
limiter's own backing store hiccupped would turn a brief Redis blip into a
total outage, which is strictly worse than serving a request unthrottled
for the (typically short) duration of that outage — especially for the
WhatsApp webhook and Explore, where "reply to the user at all" already
carries more weight elsewhere in this codebase (architecture.md/decisions.md's
"never a silently dropped message" principle) than throttling does.
"""

import logging
from dataclasses import dataclass

import redis.exceptions
from redis.asyncio import Redis

from app.core.logging import log_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    current_count: int
    limit: int


class RateLimiter:
    def __init__(self, redis: Redis, limit_per_minute: int) -> None:
        self._redis = redis
        self._limit = limit_per_minute

    async def check_and_increment(self, phone_hash: str) -> RateLimitResult:
        key = f"ratelimit:{phone_hash}"
        try:
            # INCR creates the key at 1 if absent; only set an expiry on that
            # first increment so the window is exactly 60s from first message,
            # not extended forever by an EXPIRE on every call.
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 60)
        except redis.exceptions.RedisError as exc:
            # Fail OPEN — see module docstring for why this differs from the
            # JWT-revocation check's fail-closed posture.
            log_event(
                logger,
                logging.ERROR,
                "rate limiter unavailable — failing open (request allowed unthrottled)",
                error_type=type(exc).__name__,
            )
            return RateLimitResult(allowed=True, current_count=0, limit=self._limit)
        return RateLimitResult(allowed=count <= self._limit, current_count=count, limit=self._limit)
