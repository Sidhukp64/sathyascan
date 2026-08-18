"""
Per-phone-number OTP request cooldown (Phase 6, api-design.md's
`POST /auth/link/start`). Same Redis SET-NX-EX pattern as
app/webhook/whatsapp/idempotency.py's IdempotencyGuard — this is that exact
precedent applied to a different key shape ("has this phone requested an OTP
in the last N seconds" instead of "have we seen this wamid before"), not a
new mechanism. Kept as its own tiny class rather than reusing
IdempotencyGuard directly since that class hardcodes the `idemp:wamid:` key
prefix and TTL semantics are conceptually different (dedupe vs. cooldown),
even though the underlying Redis operation is identical.
"""

from redis.asyncio import Redis


class OtpCooldownGuard:
    def __init__(self, redis: Redis, cooldown_seconds: int) -> None:
        self._redis = redis
        self._cooldown_seconds = cooldown_seconds

    async def start_cooldown_if_allowed(self, phone_hash: str) -> bool:
        """Atomically checks-and-starts the cooldown window. Returns True if
        this call was allowed to proceed (no active cooldown existed) and the
        cooldown has now been started; False if a request was already made
        too recently for this phone number."""
        key = f"otp:cooldown:{phone_hash}"
        was_set = await self._redis.set(key, "1", nx=True, ex=self._cooldown_seconds)
        return was_set is not None
