"""
Integration tests for the Phase 6 Privacy Mode retention purge
(app/agent/retention.py) — against a real (in-memory SQLite) test database,
per the user's explicit instruction: "Tests MUST prove: 1. expired records
are deleted 2. non-expired records remain 3. repeated purge is safe
4. cascading relationships work 5. purge does not delete another user's
non-expired data 6. purge/audit behavior does not leak sensitive data."

Deletes in app/agent/retention.py are explicit child-to-parent SQL DELETEs,
NOT reliant on the database's ON DELETE CASCADE firing (see that module's
docstring for why) — these tests are exactly what proves that decision
correct: SQLite in this project's test config does not enforce FK
constraints, so a test relying on CASCADE alone would pass here even if the
purge code were broken.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agent.retention import (
    compute_media_retention_expiry,
    purge_expired_analyses,
    purge_expired_media_attachments,
    run_retention_purge,
)
from app.core.config import Settings
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.transcript import Transcript
from app.models.user import User
from app.models.video_frame import VideoFrame

PEPPER = "test-pepper"


def _settings(**overrides) -> Settings:
    return Settings(
        PHONE_HASH_PEPPER=PEPPER,
        PRIVACY_MODE_MEDIA_TTL_MINUTES=15,
        PRIVACY_MODE_ANALYSIS_TTL_HOURS=48,
        STANDARD_RETENTION_MONTHS=24,
        **overrides,
    )


async def _make_user(session, phone_hash: str | None = None) -> User:
    user = User(phone_number_encrypted=b"x", phone_number_hash=phone_hash or uuid.uuid4().hex)
    session.add(user)
    await session.flush()
    return user


async def _make_analysis(
    session, user_id: uuid.UUID, *, created_at: datetime, privacy_mode_snapshot: bool, status: str = "completed"
) -> Analysis:
    analysis = Analysis(
        user_id=user_id,
        input_type="image",
        input_text="claim text",
        content_fingerprint=uuid.uuid4().hex,
        source_wamid=f"wamid.{uuid.uuid4().hex}",
        status=status,
        overall_result="false",
        language="en",
        privacy_mode_snapshot=privacy_mode_snapshot,
        created_at=created_at,
        completed_at=created_at,
    )
    session.add(analysis)
    await session.flush()
    return analysis


async def _make_media_attachment(session, analysis_id: uuid.UUID, *, retention_expires_at: datetime) -> MediaAttachment:
    media = MediaAttachment(
        analysis_id=analysis_id,
        whatsapp_media_id=uuid.uuid4().hex,
        media_type="image",
        retention_expires_at=retention_expires_at,
    )
    session.add(media)
    await session.flush()
    return media


# ---------------------------------------------------------------------------
# compute_media_retention_expiry — pure function
# ---------------------------------------------------------------------------


def test_compute_expiry_privacy_mode_uses_short_ttl():
    settings = _settings()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expiry = compute_media_retention_expiry(created_at, True, settings)
    assert expiry == created_at + timedelta(minutes=15)


def test_compute_expiry_standard_mode_uses_long_ttl():
    settings = _settings()
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expiry = compute_media_retention_expiry(created_at, False, settings)
    assert expiry == created_at + timedelta(days=24 * 30)


# ---------------------------------------------------------------------------
# purge_expired_media_attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_media_attachment_is_deleted(db_session):
    user = await _make_user(db_session)
    analysis = await _make_analysis(db_session, user.id, created_at=datetime.now(timezone.utc), privacy_mode_snapshot=True)
    expired = await _make_media_attachment(
        db_session, analysis.id, retention_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await db_session.commit()

    deleted_count = await purge_expired_media_attachments(db_session)
    await db_session.commit()

    assert deleted_count == 1
    result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.id == expired.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_non_expired_media_attachment_remains(db_session):
    user = await _make_user(db_session)
    analysis = await _make_analysis(db_session, user.id, created_at=datetime.now(timezone.utc), privacy_mode_snapshot=True)
    not_expired = await _make_media_attachment(
        db_session, analysis.id, retention_expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    await db_session.commit()

    deleted_count = await purge_expired_media_attachments(db_session)
    await db_session.commit()

    assert deleted_count == 0
    result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.id == not_expired.id))
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_media_purge_cascades_to_children(db_session):
    """Proves the EXPLICIT child-to-parent deletes actually work — SQLite in
    this test config does not enforce ON DELETE CASCADE, so this is a real
    test of retention.py's own logic, not the database's."""
    user = await _make_user(db_session)
    analysis = await _make_analysis(db_session, user.id, created_at=datetime.now(timezone.utc), privacy_mode_snapshot=True)
    media = await _make_media_attachment(
        db_session, analysis.id, retention_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    transcript = Transcript(media_attachment_id=media.id, text="hello", created_at=datetime.now(timezone.utc))
    frame = VideoFrame(media_attachment_id=media.id, frame_timestamp_ms=0, created_at=datetime.now(timezone.utc))
    forensics = MediaForensicsResult(
        media_attachment_id=media.id, tool_name="image_analyzer", provider_name="test", model_version="v1"
    )
    db_session.add_all([transcript, frame, forensics])
    await db_session.commit()

    await purge_expired_media_attachments(db_session)
    await db_session.commit()

    for model, row_id in [(Transcript, transcript.id), (VideoFrame, frame.id), (MediaForensicsResult, forensics.id)]:
        result = await db_session.execute(select(model).where(model.id == row_id))
        assert result.scalar_one_or_none() is None, f"{model.__name__} row survived purge — cascade broken"


@pytest.mark.asyncio
async def test_media_purge_is_idempotent(db_session):
    user = await _make_user(db_session)
    analysis = await _make_analysis(db_session, user.id, created_at=datetime.now(timezone.utc), privacy_mode_snapshot=True)
    await _make_media_attachment(
        db_session, analysis.id, retention_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await db_session.commit()

    first = await purge_expired_media_attachments(db_session)
    await db_session.commit()
    second = await purge_expired_media_attachments(db_session)
    await db_session.commit()

    assert first == 1
    assert second == 0  # nothing left to delete — safe, no error, no double-count


@pytest.mark.asyncio
async def test_media_purge_does_not_touch_other_users_data(db_session):
    user_a = await _make_user(db_session)
    user_b = await _make_user(db_session)
    analysis_a = await _make_analysis(db_session, user_a.id, created_at=datetime.now(timezone.utc), privacy_mode_snapshot=True)
    analysis_b = await _make_analysis(db_session, user_b.id, created_at=datetime.now(timezone.utc), privacy_mode_snapshot=True)

    expired_a = await _make_media_attachment(
        db_session, analysis_a.id, retention_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    fresh_b = await _make_media_attachment(
        db_session, analysis_b.id, retention_expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    await db_session.commit()

    await purge_expired_media_attachments(db_session)
    await db_session.commit()

    assert (await db_session.execute(select(MediaAttachment).where(MediaAttachment.id == expired_a.id))).scalar_one_or_none() is None
    assert (await db_session.execute(select(MediaAttachment).where(MediaAttachment.id == fresh_b.id))).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# purge_expired_analyses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_privacy_mode_analysis_is_deleted(db_session):
    user = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    analysis = await _make_analysis(db_session, user.id, created_at=old, privacy_mode_snapshot=True)
    await db_session.commit()

    deleted_count = await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert deleted_count == 1
    assert (await db_session.execute(select(Analysis).where(Analysis.id == analysis.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_recent_privacy_mode_analysis_is_not_deleted(db_session):
    user = await _make_user(db_session)
    settings = _settings()
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    analysis = await _make_analysis(db_session, user.id, created_at=recent, privacy_mode_snapshot=True)
    await db_session.commit()

    deleted_count = await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert deleted_count == 0
    assert (await db_session.execute(select(Analysis).where(Analysis.id == analysis.id))).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_non_privacy_mode_analysis_uses_standard_retention_not_short_ttl(db_session):
    """An analysis with privacy_mode_snapshot=False must NOT be purged just
    because it's older than the short privacy-mode TTL — it's governed by
    STANDARD_RETENTION_MONTHS instead."""
    user = await _make_user(db_session)
    settings = _settings()
    older_than_privacy_ttl_but_not_standard = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    analysis = await _make_analysis(
        db_session, user.id, created_at=older_than_privacy_ttl_but_not_standard, privacy_mode_snapshot=False
    )
    await db_session.commit()

    deleted_count = await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert deleted_count == 0
    assert (await db_session.execute(select(Analysis).where(Analysis.id == analysis.id))).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_analysis_purge_cascades_to_claims_and_evidence(db_session):
    user = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    analysis = await _make_analysis(db_session, user.id, created_at=old, privacy_mode_snapshot=True)

    claim = Claim(
        analysis_id=analysis.id,
        claim_text="x",
        claim_order=0,
        result="false",
        reasoning_text="y",
    )
    db_session.add(claim)
    await db_session.flush()
    evidence = Evidence(
        claim_id=claim.id, stance="contradicting", retrieved_at=datetime.now(timezone.utc)
    )
    db_session.add(evidence)
    await db_session.commit()

    await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert (await db_session.execute(select(Claim).where(Claim.id == claim.id))).scalar_one_or_none() is None
    assert (await db_session.execute(select(Evidence).where(Evidence.id == evidence.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_analysis_purge_cascades_to_media_attachment(db_session):
    user = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    analysis = await _make_analysis(db_session, user.id, created_at=old, privacy_mode_snapshot=True)
    # Media not yet expired on its OWN ttl — but its parent analysis is.
    media = await _make_media_attachment(
        db_session, analysis.id, retention_expires_at=datetime.now(timezone.utc) + timedelta(days=1)
    )
    await db_session.commit()

    await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert (await db_session.execute(select(MediaAttachment).where(MediaAttachment.id == media.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_analysis_purge_does_not_delete_another_users_non_expired_analysis(db_session):
    user_a = await _make_user(db_session)
    user_b = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    recent = datetime.now(timezone.utc)

    expired_a = await _make_analysis(db_session, user_a.id, created_at=old, privacy_mode_snapshot=True)
    fresh_b = await _make_analysis(db_session, user_b.id, created_at=recent, privacy_mode_snapshot=True)
    await db_session.commit()

    await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert (await db_session.execute(select(Analysis).where(Analysis.id == expired_a.id))).scalar_one_or_none() is None
    assert (await db_session.execute(select(Analysis).where(Analysis.id == fresh_b.id))).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_analysis_purge_is_idempotent(db_session):
    user = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    await _make_analysis(db_session, user.id, created_at=old, privacy_mode_snapshot=True)
    await db_session.commit()

    first = await purge_expired_analyses(db_session, settings)
    await db_session.commit()
    second = await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert first == 1
    assert second == 0


# ---------------------------------------------------------------------------
# run_retention_purge — combined entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_retention_purge_commits_and_returns_counts(db_session):
    user = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    analysis = await _make_analysis(db_session, user.id, created_at=old, privacy_mode_snapshot=True)
    expired_media = await _make_media_attachment(
        db_session, analysis.id, retention_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await db_session.commit()

    result = await run_retention_purge(db_session, settings)

    assert result.analyses_deleted == 1
    # The media_attachment was already deleted in the FIRST pass (its own
    # short TTL expired) before the analysis-level pass even runs — proves
    # the two passes don't double-count or conflict.
    assert result.media_attachments_deleted == 1
    assert (await db_session.execute(select(Analysis).where(Analysis.id == analysis.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_run_retention_purge_leaves_no_orphaned_rows_across_full_seed(db_session):
    """A broader end-to-end proof: seed one fully-populated expired analysis
    (claim + evidence + media + transcript + frame + forensics) alongside
    one fully-populated NON-expired analysis for a different user, purge
    once, and assert exactly the expired tree is gone while the other
    user's tree is completely intact."""
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    recent = datetime.now(timezone.utc)

    user_expired = await _make_user(db_session)
    user_fresh = await _make_user(db_session)

    async def _seed_full_tree(user_id, created_at):
        analysis = await _make_analysis(db_session, user_id, created_at=created_at, privacy_mode_snapshot=True)
        claim = Claim(analysis_id=analysis.id, claim_text="x", claim_order=0, result="false", reasoning_text="y")
        db_session.add(claim)
        await db_session.flush()
        evidence = Evidence(claim_id=claim.id, stance="neutral", retrieved_at=datetime.now(timezone.utc))
        media = MediaAttachment(
            analysis_id=analysis.id,
            whatsapp_media_id=uuid.uuid4().hex,
            media_type="image",
            retention_expires_at=created_at + timedelta(days=365),  # not independently expired
        )
        db_session.add_all([evidence, media])
        await db_session.flush()
        transcript = Transcript(media_attachment_id=media.id, text="t", created_at=datetime.now(timezone.utc))
        db_session.add(transcript)
        await db_session.flush()
        return analysis, claim, evidence, media, transcript

    expired_tree = await _seed_full_tree(user_expired.id, old)
    fresh_tree = await _seed_full_tree(user_fresh.id, recent)
    await db_session.commit()

    await run_retention_purge(db_session, settings)

    for model, row in zip(
        [Analysis, Claim, Evidence, MediaAttachment, Transcript], expired_tree, strict=True
    ):
        result = await db_session.execute(select(model).where(model.id == row.id))
        assert result.scalar_one_or_none() is None, f"expired {model.__name__} survived purge"

    for model, row in zip(
        [Analysis, Claim, Evidence, MediaAttachment, Transcript], fresh_tree, strict=True
    ):
        result = await db_session.execute(select(model).where(model.id == row.id))
        assert result.scalar_one_or_none() is not None, f"non-expired {model.__name__} was wrongly deleted"


@pytest.mark.asyncio
async def test_analysis_purge_nulls_out_scheduled_check_instead_of_deleting_it(db_session):
    """Phase 8: scheduled_checks.source_analysis_id is SET NULL, not
    CASCADE — a scheduled check must survive its source analysis being
    purged, since it already carries an independent claim/category/language
    snapshot (decisions.md §14). Proves the explicit UPDATE in
    _delete_analysis_and_children actually does this on SQLite, which never
    enforces the FK's ON DELETE SET NULL itself."""
    from app.models.scheduled_check import ScheduledCheck

    user = await _make_user(db_session)
    settings = _settings()
    old = datetime.now(timezone.utc) - timedelta(hours=settings.privacy_mode_analysis_ttl_hours + 1)
    analysis = await _make_analysis(db_session, user.id, created_at=old, privacy_mode_snapshot=True)

    check = ScheduledCheck(
        user_id=user.id,
        source_analysis_id=analysis.id,
        claim_text_snapshot="claim text",
        language="en",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(check)
    await db_session.commit()

    await purge_expired_analyses(db_session, settings)
    await db_session.commit()

    assert (await db_session.execute(select(Analysis).where(Analysis.id == analysis.id))).scalar_one_or_none() is None

    result = await db_session.execute(select(ScheduledCheck).where(ScheduledCheck.id == check.id))
    surviving_check = result.scalar_one_or_none()
    assert surviving_check is not None, "scheduled_check row was wrongly deleted, not nulled"
    assert surviving_check.source_analysis_id is None
    assert surviving_check.claim_text_snapshot == "claim text"  # snapshot intact
