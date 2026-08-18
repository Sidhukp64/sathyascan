"""
Redis-based idempotency guard for inbound WhatsApp messages (decisions.md
§1/§12, whatsapp-integration.md). Phase 1 has no database, so this is the
only dedupe mechanism — the eventual `analyses.source_wamid UNIQUE`
constraint arrives with the database in a later phase and will be a second,
durable layer on top of this one, not a replacement for it.
"""

from redis.asyncio import Redis


class IdempotencyGuard:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def is_duplicate(self, wamid: str) -> bool:
        """Atomically marks wamid as seen; returns True if it was ALREADY
        seen (i.e. this call is a duplicate and should be skipped)."""
        key = f"idemp:wamid:{wamid}"
        was_set = await self._redis.set(key, "1", nx=True, ex=self._ttl_seconds)
        # redis-py returns True on success, None if the key already existed.
        return was_set is None
