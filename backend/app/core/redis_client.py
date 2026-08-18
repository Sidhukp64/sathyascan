"""
Redis client factory. Phase 1 uses Redis only — for idempotency and rate
limiting (decisions.md §1, §12) — there is no database yet, so nothing here
is durable storage.

Connection is lazy: redis.asyncio.Redis.from_url() does not connect until the
first command is issued, so constructing this client at app startup never
fails just because Redis isn't reachable yet (useful for tests, which override
the get_redis dependency instead of relying on this client at all).
"""

from redis.asyncio import Redis


def build_redis_client(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, decode_responses=True)
