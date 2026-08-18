"""
Privacy Mode retention purge (decisions.md §8, database-schema.md's Privacy
Mode -> schema behavior table, docs/phased-plan.md's Phase 6 row: "Privacy
Mode wired to real config-driven retention/deletion jobs with a
verification check that deletion actually occurred").

Purges METADATA ROWS ONLY — Phase 3 never persisted raw media blobs
(app/models/media_attachment.py's docstring: `storage_path` stays
permanently NULL), so there is no blob store to purge separately; "purge"
here means removing the DB rows themselves.

Two independent expiry tracks, matching database-schema.md:
1. Media attachments: `media_attachments.retention_expires_at`, populated
   at creation time by Image/Audio/VideoPipeline from `privacy_mode_snapshot`
   + PRIVACY_MODE_MEDIA_TTL_MINUTES / STANDARD_RETENTION_MONTHS (see
   compute_media_retention_expiry below, called from each pipeline).
2. Whole analyses: computed LIVE from `created_at` + the applicable TTL,
   scoped by `analyses.privacy_mode_snapshot` (PRIVACY_MODE_ANALYSIS_TTL_HOURS
   for privacy-mode analyses, STANDARD_RETENTION_MONTHS otherwise) — no
   separate expiry column needed; this is a straightforward age check,
   unlike media_attachments which has other lifecycle reasons to carry its
   own explicit column.

**Deletes are explicit and child-to-parent, executed IN PYTHON — deliberately
NOT relying on the database's ON DELETE CASCADE firing.** This codebase never
sets `PRAGMA foreign_keys=ON` for its SQLite test engine (grepped, confirmed
absent), so SQLite does not enforce FK constraints or CASCADE actions by
default even though every FK here declares `ondelete="CASCADE"` — that
CASCADE is real on production Postgres but silently inert in this project's
own SQLite-backed test suite. Trusting it here would make this code look
correct in tests while being unverified in the one place the user's
instructions require proof ("Tests MUST prove cascading relationships
work"). Explicit ordered deletes are correct on both databases identically
and don't depend on that PRAGMA at all.

Idempotent: every delete is scoped by an expiry predicate evaluated FRESH
each call (not a work queue that could be double-processed) — running the
purge twice back-to-back with nothing newly expired deletes nothing the
second time, and running it a third time after new data expires only
deletes that new data.

**Phase 8 note**: `delete_analysis_and_children`/`delete_media_attachment_and_children`
are public (not `_`-prefixed) specifically so `app/agent/account_deletion.py`
can reuse them verbatim for full account erasure — same explicit-delete
logic, just triggered by a user's own deletion request instead of a TTL.
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.analysis import Analysis
from app.models.appeal import Appeal
from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.media_attachment import MediaAttachment
from app.models.media_forensics_result import MediaForensicsResult
from app.models.moderation_report import ModerationReport
from app.models.safety_gate_event import SafetyGateEvent
from app.models.scheduled_check import ScheduledCheck
from app.models.transcript import Transcript
from app.models.url_safety_scan import UrlSafetyScan
from app.models.video_frame import VideoFrame


class PurgeResult(NamedTuple):
    media_attachments_deleted: int
    analyses_deleted: int


def compute_media_retention_expiry(
    created_at: datetime, privacy_mode_snapshot: bool, settings: Settings
) -> datetime:
    """Called by Image/Audio/VideoPipeline at MediaAttachment creation time
    to populate `retention_expires_at`. Approximates "N months" as N*30 days
    — consistent with how the analysis-level cutoff below computes the same
    thing; a calendar-exact month isn't worth the added complexity for a
    retention window measured in months, not days."""
    if privacy_mode_snapshot:
        return created_at + timedelta(minutes=settings.privacy_mode_media_ttl_minutes)
    return created_at + timedelta(days=settings.standard_retention_months * 30)


async def delete_media_attachment_and_children(session: AsyncSession, media_attachment_id: UUID) -> None:
    await session.execute(
        delete(MediaForensicsResult).where(MediaForensicsResult.media_attachment_id == media_attachment_id)
    )
    await session.execute(delete(Transcript).where(Transcript.media_attachment_id == media_attachment_id))
    await session.execute(delete(VideoFrame).where(VideoFrame.media_attachment_id == media_attachment_id))
    await session.execute(delete(SafetyGateEvent).where(SafetyGateEvent.media_attachment_id == media_attachment_id))
    await session.execute(delete(MediaAttachment).where(MediaAttachment.id == media_attachment_id))


async def purge_expired_media_attachments(session: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    result = await session.execute(
        select(MediaAttachment.id).where(MediaAttachment.retention_expires_at <= now)
    )
    ids = [row[0] for row in result.all()]
    for media_attachment_id in ids:
        await delete_media_attachment_and_children(session, media_attachment_id)
    return len(ids)


async def delete_analysis_and_children(session: AsyncSession, analysis_id: UUID) -> None:
    claim_ids_result = await session.execute(select(Claim.id).where(Claim.analysis_id == analysis_id))
    claim_ids = [row[0] for row in claim_ids_result.all()]
    if claim_ids:
        await session.execute(delete(Evidence).where(Evidence.claim_id.in_(claim_ids)))
    await session.execute(delete(Claim).where(Claim.analysis_id == analysis_id))

    media_ids_result = await session.execute(
        select(MediaAttachment.id).where(MediaAttachment.analysis_id == analysis_id)
    )
    media_ids = [row[0] for row in media_ids_result.all()]
    for media_attachment_id in media_ids:
        await delete_media_attachment_and_children(session, media_attachment_id)

    await session.execute(delete(SafetyGateEvent).where(SafetyGateEvent.analysis_id == analysis_id))
    await session.execute(delete(UrlSafetyScan).where(UrlSafetyScan.analysis_id == analysis_id))

    # Phase 8 — scheduled_checks.source_analysis_id is SET NULL, not
    # CASCADE (app/models/scheduled_check.py's docstring: a scheduled check
    # must survive its source analysis being purged, since it already
    # carries its own independent claim/category/language snapshot). Real
    # Postgres would null this out automatically on delete; this project's
    # SQLite test engine never enforces FKs at all (see this module's own
    # docstring), so the null-out is done explicitly here, same reasoning
    # as every other delete in this function.
    await session.execute(
        update(ScheduledCheck)
        .where(ScheduledCheck.source_analysis_id == analysis_id)
        .values(source_analysis_id=None)
    )

    # Phase 9 — appeals.analysis_id is CASCADE (an appeal about a since-
    # purged analysis has no purpose — app/models/appeal.py's docstring);
    # moderation_reports.target_analysis_id is SET NULL (the report record
    # itself still has value even once its subject is gone, same reasoning
    # as scheduled_checks above). Both done explicitly here, same "never
    # trust the DB's ON DELETE action" discipline as every other delete in
    # this function.
    await session.execute(delete(Appeal).where(Appeal.analysis_id == analysis_id))
    await session.execute(
        update(ModerationReport)
        .where(ModerationReport.target_analysis_id == analysis_id)
        .values(target_analysis_id=None)
    )

    await session.execute(delete(Analysis).where(Analysis.id == analysis_id))


async def purge_expired_analyses(session: AsyncSession, settings: Settings, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    privacy_cutoff = now - timedelta(hours=settings.privacy_mode_analysis_ttl_hours)
    standard_cutoff = now - timedelta(days=settings.standard_retention_months * 30)

    result = await session.execute(
        select(Analysis.id).where(
            (
                Analysis.privacy_mode_snapshot.is_(True) & (Analysis.created_at <= privacy_cutoff)
            )
            | (
                Analysis.privacy_mode_snapshot.is_(False) & (Analysis.created_at <= standard_cutoff)
            )
        )
    )
    ids = [row[0] for row in result.all()]
    for analysis_id in ids:
        await delete_analysis_and_children(session, analysis_id)
    return len(ids)


async def run_retention_purge(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> PurgeResult:
    """Single idempotent entry point — called by both the periodic
    background task (main.py lifespan) and directly by tests. Deletes
    media_attachments first, then analyses (an analysis whose media JUST
    expired independently, before the analysis itself is old enough to
    expire, is handled correctly either order — the two passes are
    independent, not sequential dependencies of each other)."""
    now = now or datetime.now(timezone.utc)
    media_deleted = await purge_expired_media_attachments(session, now)
    analyses_deleted = await purge_expired_analyses(session, settings, now)
    await session.commit()
    return PurgeResult(media_attachments_deleted=media_deleted, analyses_deleted=analyses_deleted)
