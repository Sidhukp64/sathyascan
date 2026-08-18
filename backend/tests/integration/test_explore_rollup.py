"""
Phase 8 Explore rollup (app/agent/explore_rollup.py) against a real
(in-memory SQLite) database. Proves the privacy-filtering rule
(decisions.md §8/database-schema.md: Privacy-Mode-ON claims are excluded
entirely) actually holds in the query, not just in a comment.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.explore_rollup import run_explore_rollup
from app.core.config import Settings
from app.db.base import Base
from app.db.session import build_sessionmaker
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.explore_claim_cluster import ExploreClaimCluster
from app.models.user import User


def _settings(**overrides) -> Settings:
    return Settings(PHONE_HASH_PEPPER="p", EXPLORE_ROLLUP_LOOKBACK_HOURS=720, **overrides)


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


async def _seed(
    session,
    *,
    claim_text: str,
    result: str,
    privacy_mode: bool,
    status: str = "completed",
    category: str = "health",
    confidence: float = 0.8,
    evidence_count: int = 2,
) -> None:
    user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
    session.add(user)
    await session.flush()

    analysis = Analysis(
        user_id=user.id,
        input_type="text",
        input_text=claim_text,
        content_fingerprint=uuid.uuid4().hex,
        source_wamid=f"wamid.{uuid.uuid4().hex}",
        status=status,
        overall_result=result,
        language="en",
        privacy_mode_snapshot=privacy_mode,
    )
    session.add(analysis)
    await session.flush()

    claim = Claim(
        analysis_id=analysis.id,
        claim_text=claim_text,
        claim_order=0,
        result=result,
        investigation_complete=True,
        claim_confidence=confidence,
        reasoning_text="x",
        category=category,
    )
    session.add(claim)
    await session.flush()

    for _ in range(evidence_count):
        session.add(Evidence(claim_id=claim.id, stance="neutral", retrieved_at=datetime.now(timezone.utc)))

    await session.commit()


@pytest.mark.asyncio
async def test_privacy_mode_on_claims_are_never_rolled_up(db_session):
    await _seed(db_session, claim_text="The moon landing was faked", result="false", privacy_mode=True)

    count = await run_explore_rollup(db_session, _settings())

    assert count == 0
    result = await db_session.execute(select(ExploreClaimCluster))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_privacy_mode_off_claim_is_rolled_up(db_session):
    await _seed(db_session, claim_text="Water boils at 100 degrees Celsius", result="verified", privacy_mode=False)

    count = await run_explore_rollup(db_session, _settings())

    assert count == 1
    result = await db_session.execute(select(ExploreClaimCluster))
    cluster = result.scalars().one()
    assert cluster.credibility_status == "verified"
    assert cluster.check_count == 1
    assert cluster.source_count == 2


@pytest.mark.asyncio
async def test_identical_claims_cluster_together_and_increment_check_count(db_session):
    await _seed(db_session, claim_text="5G towers cause illness", result="false", privacy_mode=False)
    await _seed(db_session, claim_text="5g towers CAUSE illness!!", result="false", privacy_mode=False)  # near-identical

    count = await run_explore_rollup(db_session, _settings())

    assert count == 1  # one cluster, not two
    result = await db_session.execute(select(ExploreClaimCluster))
    cluster = result.scalars().one()
    assert cluster.check_count == 2


@pytest.mark.asyncio
async def test_rollup_is_idempotent(db_session):
    await _seed(db_session, claim_text="A claim", result="unverified", privacy_mode=False)

    first = await run_explore_rollup(db_session, _settings())
    second = await run_explore_rollup(db_session, _settings())

    assert first == 1
    assert second == 1  # recomputed, not double-counted
    result = await db_session.execute(select(ExploreClaimCluster))
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_incomplete_investigation_claims_are_excluded(db_session):
    """insufficient_evidence claims (investigation_complete=False) are never
    surfaced on Explore — an incomplete investigation is not a public,
    citable result."""
    user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
    db_session.add(user)
    await db_session.flush()
    analysis = Analysis(
        user_id=user.id,
        input_type="text",
        input_text="x",
        content_fingerprint=uuid.uuid4().hex,
        source_wamid=f"wamid.{uuid.uuid4().hex}",
        status="completed",
        overall_result="insufficient_evidence",
        language="en",
        privacy_mode_snapshot=False,
    )
    db_session.add(analysis)
    await db_session.flush()
    db_session.add(
        Claim(
            analysis_id=analysis.id,
            claim_text="x",
            claim_order=0,
            result="insufficient_evidence",
            investigation_complete=False,
            incomplete_reason="timeout",
            reasoning_text="x",
        )
    )
    await db_session.commit()

    count = await run_explore_rollup(db_session, _settings())
    assert count == 0


@pytest.mark.asyncio
async def test_deleted_analysis_is_excluded(db_session):
    await _seed(db_session, claim_text="deleted claim", result="false", privacy_mode=False)
    result = await db_session.execute(select(Analysis))
    analysis = result.scalars().one()
    analysis.is_deleted = True
    await db_session.commit()

    count = await run_explore_rollup(db_session, _settings())
    assert count == 0


@pytest.mark.asyncio
async def test_claims_outside_lookback_window_are_excluded(db_session):
    await _seed(db_session, claim_text="old claim", result="false", privacy_mode=False)
    result = await db_session.execute(select(Analysis))
    analysis = result.scalars().one()
    analysis.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    await db_session.commit()

    count = await run_explore_rollup(db_session, _settings())  # default 720h (30 day) lookback
    assert count == 0
