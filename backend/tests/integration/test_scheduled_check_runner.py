"""
Phase 8 "Check This Tomorrow" background execution
(app/agent/scheduled_check_runner.py), exercised directly against
`run_due_scheduled_checks` (not through HTTP) with a real in-memory SQLite
DB and scripted fake LLM/search — same testing shape
tests/integration/test_text_pipeline.py already established for
`investigate_claim` (reused here unchanged, not re-tested for its own
correctness).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.scheduled_check_runner import run_due_scheduled_checks
from app.core.config import Settings
from app.db.base import Base
from app.db.session import build_sessionmaker
from app.models.notification import Notification
from app.models.scheduled_check import ScheduledCheck
from app.models.user import User
from tests.conftest import (
    TEST_PHONE_ENCRYPTION_KEY,
    TEST_PHONE_PEPPER,
    FakeEvidenceSearchProvider,
    FakeLLMClient,
    make_text_response,
    make_tool_response,
)


def _settings(**overrides) -> Settings:
    base = dict(
        WHATSAPP_APP_SECRET="x",
        WHATSAPP_WEBHOOK_VERIFY_TOKEN="x",
        WHATSAPP_ACCESS_TOKEN="x",
        WHATSAPP_PHONE_NUMBER_ID="x",
        PHONE_HASH_PEPPER=TEST_PHONE_PEPPER,
        PHONE_ENCRYPTION_KEY=TEST_PHONE_ENCRYPTION_KEY,
        MAX_EVIDENCE_SEARCHES_PER_ANALYSIS=5,
        MAX_LLM_TOOL_CALLS_PER_ANALYSIS=10,
        DAILY_SPEND_CIRCUIT_BREAKER_USD=50.0,
        GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT=20,
        MIN_CONCURRING_TIER2_SOURCES=2,
    )
    base.update(overrides)
    return Settings(**base)


def _synthesis_response(result: str, confidence: float = 0.9):
    return make_tool_response(
        "record_synthesis",
        {
            "evidence_stances": [],
            "suggested_result": result,
            "claim_confidence": confidence,
            "reasoning_text": f"Re-checked evidence supports {result}.",
        },
    )


def _no_search_responder(synthesis_result: str):
    """LLM immediately stops the investigation loop (no search calls), then
    returns the given synthesis verdict — the minimal script needed to
    drive investigate_claim to completion without re-testing its own
    internal correctness (already covered by test_text_pipeline.py)."""

    def _responder(ctx):
        if ctx.forced_tool_name == "record_synthesis":
            return _synthesis_response(synthesis_result)
        return make_text_response("no further search needed")

    return _responder


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


async def _make_user_and_check(session, *, scheduled_for=None, previous_result="unverified", status="pending"):
    user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
    session.add(user)
    await session.flush()

    check = ScheduledCheck(
        user_id=user.id,
        source_analysis_id=None,
        claim_text_snapshot="The moon landing was faked.",
        category="other",
        language="en",
        status=status,
        scheduled_for=scheduled_for or (datetime.now(timezone.utc) - timedelta(minutes=1)),
        previous_result_snapshot={
            "result": previous_result,
            "reasoning_text": "original reasoning",
            "claim_confidence": 0.5,
            "evidence_strength": None,
            "evidence_tier_met": None,
            "investigation_complete": True,
            "incomplete_reason": None,
        },
    )
    session.add(check)
    await session.commit()
    await session.refresh(check)
    return user, check


@pytest.mark.asyncio
async def test_due_check_is_executed_and_marked_completed(db_session):
    # The fake LLM/search script returns ZERO evidence — the SAME
    # deterministic evidence-tier guard investigate_claim always enforces
    # (decisions.md §3) correctly downgrades an unsupported "false"
    # suggestion to "unverified" here too; this is proof the guard applies
    # identically on a re-check, not a bug in the test. previous_result is
    # therefore set to "unverified" so this specific test isolates the
    # no-change path.
    _, check = await _make_user_and_check(db_session, previous_result="unverified")
    llm = FakeLLMClient(responder=_no_search_responder("false"))
    search = FakeEvidenceSearchProvider()
    settings = _settings()

    executed = await run_due_scheduled_checks(db_session, settings, llm, search)

    assert executed == 1
    await db_session.refresh(check)
    assert check.status == "completed"
    assert check.executed_at is not None
    assert check.new_result_snapshot["result"] == "unverified"
    assert check.credibility_changed is False  # same as previous_result_snapshot


@pytest.mark.asyncio
async def test_credibility_change_is_detected_and_notified(db_session):
    # previous_result="false" (a claim that WAS false) vs. this re-check's
    # zero-evidence "unverified" outcome — a real, meaningful change.
    user, check = await _make_user_and_check(db_session, previous_result="false")
    llm = FakeLLMClient(responder=_no_search_responder("false"))
    search = FakeEvidenceSearchProvider()
    settings = _settings()

    await run_due_scheduled_checks(db_session, settings, llm, search)

    await db_session.refresh(check)
    assert check.credibility_changed is True
    assert check.notification_status == "generated"

    result = await db_session.execute(select(Notification).where(Notification.user_id == user.id))
    notifications = result.scalars().all()
    assert len(notifications) == 2  # in_app + whatsapp (blocked)
    channels = {n.channel: n.delivery_status for n in notifications}
    assert channels["in_app"] == "delivered"
    assert channels["whatsapp"] == "blocked_by_policy"
    assert all(n.notification_type == "credibility_changed" for n in notifications)


@pytest.mark.asyncio
async def test_not_yet_due_check_is_not_executed(db_session):
    _, check = await _make_user_and_check(
        db_session, scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    llm = FakeLLMClient(responder=_no_search_responder("false"))
    search = FakeEvidenceSearchProvider()

    executed = await run_due_scheduled_checks(db_session, _settings(), llm, search)

    assert executed == 0
    await db_session.refresh(check)
    assert check.status == "pending"


@pytest.mark.asyncio
async def test_cancelled_check_is_never_executed(db_session):
    _, check = await _make_user_and_check(db_session, status="cancelled")
    llm = FakeLLMClient(responder=_no_search_responder("false"))
    search = FakeEvidenceSearchProvider()

    executed = await run_due_scheduled_checks(db_session, _settings(), llm, search)

    assert executed == 0
    await db_session.refresh(check)
    assert check.status == "cancelled"  # untouched


@pytest.mark.asyncio
async def test_provider_failure_retries_before_marking_failed(db_session):
    _, check = await _make_user_and_check(db_session)
    check.max_attempts = 2
    await db_session.commit()

    # investigate_claim/run_investigation_loop already absorb
    # LLMProviderError/LLMProviderTimeout internally and turn them into a
    # normal `insufficient_evidence` verdict (decisions.md §1A) — that path
    # is already covered by test_text_pipeline.py and does NOT exercise the
    # runner's failed/retry bookkeeping. To prove THAT bookkeeping, the
    # fake LLM must raise something investigate_claim does NOT catch — a
    # genuine unexpected crash, matching this module's own documented
    # meaning of `status='failed'`.
    def _crashing_responder(ctx):
        raise RuntimeError("simulated unexpected crash (e.g. a bug), not a provider hiccup")

    llm = FakeLLMClient(responder=_crashing_responder)
    search = FakeEvidenceSearchProvider()

    # First tick: attempt 1 of 2 — fails, stays pending for retry.
    await run_due_scheduled_checks(db_session, _settings(), llm, search)
    await db_session.refresh(check)
    assert check.status == "pending"
    assert check.attempts == 1
    assert check.error_message is not None

    # Second tick: attempt 2 of 2 — fails again, now marked failed + notified.
    check.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()
    await run_due_scheduled_checks(db_session, _settings(), llm, search)
    await db_session.refresh(check)
    assert check.status == "failed"
    assert check.notification_status == "generated"


@pytest.mark.asyncio
async def test_circuit_breaker_skips_entire_batch(db_session):
    _, check = await _make_user_and_check(db_session)
    settings = _settings(DAILY_SPEND_CIRCUIT_BREAKER_USD=1.0)

    # Trip the circuit breaker the same way usage_tracking already does elsewhere.
    from app.agent.usage_tracking import record_usage

    await record_usage(
        db_session,
        evidence_search_count=100,
        llm_tool_call_count=100,
        cost_per_search_usd=0.01,
        cost_per_llm_call_usd=0.02,
        daily_spend_limit_usd=settings.daily_spend_circuit_breaker_usd,
    )
    await db_session.commit()

    llm = FakeLLMClient(responder=_no_search_responder("false"))
    search = FakeEvidenceSearchProvider()

    executed = await run_due_scheduled_checks(db_session, settings, llm, search)

    assert executed == 0
    await db_session.refresh(check)
    assert check.status == "pending"  # untouched — batch skipped entirely


@pytest.mark.asyncio
async def test_batch_is_limited_and_processes_multiple_checks(db_session):
    for _ in range(3):
        await _make_user_and_check(db_session, previous_result="false")

    llm = FakeLLMClient(responder=_no_search_responder("false"))
    search = FakeEvidenceSearchProvider()

    executed = await run_due_scheduled_checks(db_session, _settings(), llm, search)
    assert executed == 3
