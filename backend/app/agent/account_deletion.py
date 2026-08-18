"""
Phase 8 full account deletion (roadmap §8.4, decisions.md §7's "full account
deletion... cascades every owned row... and irreversibly re-hashes the
phone identifier so it can no longer be correlated post-deletion" —
designed since the original architecture pass, never built until now).

Reuses `app.agent.retention.delete_analysis_and_children`/
`delete_media_attachment_and_children` verbatim for every analysis the
account owns — the exact same explicit, ordered, child-to-parent deletes
the retention purge already uses (and for the same reason: this project's
SQLite test engine never enforces `ON DELETE CASCADE`, so trusting it here
would leave the one thing "Add security and privacy tests" is supposed to
prove unverified).

**Session/token invalidation reuses existing infrastructure, not a new
mechanism**: JWTs are stateless and this codebase has no registry of every
token ever issued to a user (only a Redis revocation-list keyed by
individual `jti`, populated on logout — see app/core/jwt_auth.py), so there
is no way to enumerate and revoke "all of this user's tokens" one by one.
Setting `users.is_deleted = True` achieves the same effect for free:
`app/api/v1/deps.py::get_current_user` already rejects ANY token — however
recently issued, whatever its own expiry — the instant `is_deleted` is true,
because it loads and checks the live `users` row on every authenticated
request, not just at login. This is the exact mechanism Phase 6 already
built for a different reason (an account that no longer exists), reused
here unchanged.

**Anonymization, not a hard-delete of the `users` row itself**: matches
decisions.md §7's "re-hashes the phone identifier" language precisely — the
row survives (so `phone_number_hash`'s UNIQUE constraint and any other
integrity assumptions never break), but `phone_number_hash`/
`phone_number_encrypted` are overwritten with values derived from a random
UUID, never the real number, and never reversible back to it.

**`audit_log` entries survive deletion, deliberately** — that table has no
FK to `users` at all (`entity_id` is a loose UUID column, not a foreign
key), so the "this account was deleted, and when" record persists for
accountability even though the account itself is gone. The `notify_account_
deleted` helper in app/agent/notifications.py exists but is intentionally
NOT called from this flow: any notification row would itself be deleted a
few lines later along with every other row this user owns, making a
write-then-immediately-delete pointless — the audit_log entry plus the API
response IS the confirmation.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.agent.retention import delete_analysis_and_children
from app.core.config import Settings
from app.core.encryption import encrypt_phone_number
from app.core.security import hash_phone_number
from app.models.analysis import Analysis
from app.models.analysis_session import AnalysisSession
from app.models.appeal import Appeal
from app.models.dashboard_account import DashboardAccount
from app.models.moderation_report import ModerationReport
from app.models.notification import Notification
from app.models.otp_verification import OtpVerification
from app.models.scheduled_check import ScheduledCheck
from app.models.user import User


@dataclass
class AccountDeletionSummary:
    analyses_deleted: int
    scheduled_checks_deleted: int
    sessions_deleted: int
    notifications_deleted: int
    moderation_reports_deleted: int


async def delete_account(session: AsyncSession, settings: Settings, user: User) -> AccountDeletionSummary:
    # Audit BEFORE anonymizing — entity_id=user.id is only meaningful now,
    # and audit_log has no FK to users so this row survives what follows.
    await write_audit_log(
        session,
        actor_type="user",
        action="account_deleted",
        entity_type="user",
        entity_id=user.id,
    )

    scheduled_checks_result = await session.execute(
        select(ScheduledCheck.id).where(ScheduledCheck.user_id == user.id)
    )
    scheduled_checks_deleted = len(scheduled_checks_result.all())
    await session.execute(delete(ScheduledCheck).where(ScheduledCheck.user_id == user.id))

    analyses_result = await session.execute(select(Analysis.id).where(Analysis.user_id == user.id))
    analysis_ids = [row[0] for row in analyses_result.all()]
    for analysis_id in analysis_ids:
        await delete_analysis_and_children(session, analysis_id)

    sessions_result = await session.execute(select(AnalysisSession.id).where(AnalysisSession.user_id == user.id))
    sessions_deleted = len(sessions_result.all())
    await session.execute(delete(AnalysisSession).where(AnalysisSession.user_id == user.id))

    notifications_result = await session.execute(select(Notification.id).where(Notification.user_id == user.id))
    notifications_deleted = len(notifications_result.all())
    await session.execute(delete(Notification).where(Notification.user_id == user.id))

    # Phase 9 — appeals the user FILED are already gone: they can only ever
    # be about the user's OWN analyses (enforced at creation time,
    # app/api/v1/routers/appeals.py), and every one of those analyses was
    # just deleted above via delete_analysis_and_children, which explicitly
    # deletes appeals scoped to that analysis_id (see
    # app/agent/retention.py). Moderation reports the user SUBMITTED (as
    # reporter) are a genuinely separate case — they can be about someone
    # ELSE's content, so they don't get cleaned up as a side effect of
    # deleting this user's own analyses and need their own explicit delete.
    moderation_reports_result = await session.execute(
        select(ModerationReport.id).where(ModerationReport.reporter_user_id == user.id)
    )
    moderation_reports_deleted = len(moderation_reports_result.all())
    await session.execute(delete(ModerationReport).where(ModerationReport.reporter_user_id == user.id))

    await session.execute(delete(OtpVerification).where(OtpVerification.user_id == user.id))
    await session.execute(delete(DashboardAccount).where(DashboardAccount.user_id == user.id))

    # Anonymize, don't hard-delete the users row — decisions.md §7.
    placeholder = f"deleted-{uuid4()}"
    user.phone_number_hash = hash_phone_number(placeholder, settings.phone_hash_pepper)
    user.phone_number_encrypted = encrypt_phone_number(placeholder, settings.phone_encryption_key)
    user.is_deleted = True
    user.deleted_at = datetime.now(timezone.utc)
    user.preferred_language = "en"
    user.auto_detect_language = True
    user.privacy_mode = True

    await session.commit()

    return AccountDeletionSummary(
        analyses_deleted=len(analysis_ids),
        scheduled_checks_deleted=scheduled_checks_deleted,
        sessions_deleted=sessions_deleted,
        notifications_deleted=notifications_deleted,
        moderation_reports_deleted=moderation_reports_deleted,
    )
