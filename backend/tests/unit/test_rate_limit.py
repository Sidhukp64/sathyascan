import fakeredis.aioredis
import pytest
import redis.exceptions as redis_exceptions  # aliased — this file also defines a `redis` fixture below,
# whose module-level `async def redis(): ...` binding would otherwise shadow a bare `import redis.exceptions`.

from app.core.rate_limit import RateLimiter
from app.webhook.whatsapp.idempotency import IdempotencyGuard


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()


@pytest.mark.asyncio
async def test_allows_up_to_the_limit(redis):
    limiter = RateLimiter(redis, limit_per_minute=3)

    r1 = await limiter.check_and_increment("hash-a")
    r2 = await limiter.check_and_increment("hash-a")
    r3 = await limiter.check_and_increment("hash-a")

    assert r1.allowed and r2.allowed and r3.allowed
    assert r3.current_count == 3


@pytest.mark.asyncio
async def test_blocks_beyond_the_limit(redis):
    limiter = RateLimiter(redis, limit_per_minute=2)

    await limiter.check_and_increment("hash-b")
    await limiter.check_and_increment("hash-b")
    r3 = await limiter.check_and_increment("hash-b")

    assert r3.allowed is False
    assert r3.current_count == 3
    assert r3.limit == 2


@pytest.mark.asyncio
async def test_different_senders_have_independent_limits(redis):
    limiter = RateLimiter(redis, limit_per_minute=1)

    r_a = await limiter.check_and_increment("hash-a")
    r_b = await limiter.check_and_increment("hash-b")

    assert r_a.allowed and r_b.allowed


@pytest.mark.asyncio
async def test_window_has_an_expiry_set(redis):
    limiter = RateLimiter(redis, limit_per_minute=5)

    await limiter.check_and_increment("hash-c")

    ttl = await redis.ttl("ratelimit:hash-c")
    assert 0 < ttl <= 60


class _BrokenRedis:
    """A minimal stand-in that raises on the exact call
    RateLimiter.check_and_increment makes — proves the fail-open behavior
    without needing a real, genuinely-unreachable Redis server (which
    `fakeredis` structurally cannot simulate — see app/core/rate_limit.py's
    Phase 10 docstring for why this bug was invisible to every other test
    in this suite until a real uvicorn boot smoke test caught it)."""

    async def incr(self, key: str):
        raise redis_exceptions.ConnectionError("simulated Redis outage")


@pytest.mark.asyncio
async def test_redis_outage_fails_open_not_with_an_unhandled_exception():
    limiter = RateLimiter(_BrokenRedis(), limit_per_minute=5)

    result = await limiter.check_and_increment("hash-outage")

    assert result.allowed is True


class TestIdempotencyGuard:
    @pytest.mark.asyncio
    async def test_first_occurrence_is_not_duplicate(self, redis):
        guard = IdempotencyGuard(redis, ttl_seconds=60)
        assert await guard.is_duplicate("wamid.1") is False

    @pytest.mark.asyncio
    async def test_second_occurrence_is_duplicate(self, redis):
        guard = IdempotencyGuard(redis, ttl_seconds=60)
        await guard.is_duplicate("wamid.2")
        assert await guard.is_duplicate("wamid.2") is True

    @pytest.mark.asyncio
    async def test_different_wamids_are_independent(self, redis):
        guard = IdempotencyGuard(redis, ttl_seconds=60)
        assert await guard.is_duplicate("wamid.3") is False
        assert await guard.is_duplicate("wamid.4") is False
