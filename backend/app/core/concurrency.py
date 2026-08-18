"""
Global concurrency protection for the LLM/evidence-search-heavy portion of
the pipeline (decisions.md §1/§12), independent of per-user rate limiting.

Redis INCR/DECR based, not asyncio.Semaphore, so the limit holds across
multiple worker processes (an in-process semaphore wouldn't). A crash mid-
analysis could in principle leak a slot until the key's safety TTL expires —
acceptable for Phase 2's BackgroundTasks-based execution; proper
release-guaranteed semantics belong with a real task queue (Celery, Phase 3+).
"""

from redis.asyncio import Redis

_KEY = "concurrency:llm_search"
_SAFETY_TTL_SECONDS = 300  # caps how long a leaked slot (crash mid-analysis) can linger


class ConcurrencyLimitReachedError(Exception):
    pass


class GlobalConcurrencyGuard:
    def __init__(self, redis: Redis, limit: int) -> None:
        self._redis = redis
        self._limit = limit

    async def acquire(self) -> None:
        count = await self._redis.incr(_KEY)
        if count == 1:
            await self._redis.expire(_KEY, _SAFETY_TTL_SECONDS)
        if count > self._limit:
            await self._redis.decr(_KEY)
            raise ConcurrencyLimitReachedError()

    async def release(self) -> None:
        new_count = await self._redis.decr(_KEY)
        if new_count < 0:
            # Defensive: never let the counter go negative from a double-release.
            await self._redis.set(_KEY, 0)
