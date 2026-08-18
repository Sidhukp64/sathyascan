"""
scheduled_checks — Phase 8 "Check This Tomorrow" (docs/decisions.md §14,
docs/database-schema.md's original design-reference section).

decisions.md §14's data-handling constraints, honored directly in this
model's shape: under Privacy Mode ON, raw media is never retained for the
re-check (no media FK here at all — only `source_analysis_id`, and the
re-check re-derives claim text from `analyses.input_text`/the linked
claim, never a media blob); only the minimum claim snapshot is stored
(`claim_text_snapshot`); evidence is re-checked at EXECUTION time, not
replayed (`app/agent/scheduled_check_runner.py` re-runs real evidence
retrieval); `previous_result_snapshot`/`new_result_snapshot` exist
specifically so the response can show what changed.

**decisions.md §4 — proactive WhatsApp notification is NOT wired to
actually send in this build.** `notification_status` tracks whether a
notification WOULD have been generated/sent (for tests and future
activation), never whether a real WhatsApp message was delivered — see
app/agent/notifications.py's module docstring for the full, honest
explanation of what's blocked and why.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class ScheduledCheck(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "scheduled_checks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL, not CASCADE — deliberate. The whole point of this table is
    # that a scheduled check can run LATER using only its OWN snapshot
    # (claim_text_snapshot/category/language below), independent of whether
    # the source analysis still exists. Privacy Mode's short retention
    # window (media in 15min, analyses in 48h) means a Privacy-Mode-ON
    # user's source analysis will very often be purged before "tomorrow"
    # ever arrives — CASCADE would silently delete the scheduled check too,
    # defeating decisions.md §14's own reasoning ("only the minimum required
    # claim text/snapshot is stored... not a full copy of the original
    # analysis" — precisely so the snapshot can outlive the original row).
    source_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Minimum-required snapshot only (decisions.md §14) — never the original
    # media, never a full copy of the original analysis row.
    claim_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    # pending | running | completed | failed | cancelled

    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    previous_result_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_result_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    credibility_changed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "would a notification have fired" — never "was a real WhatsApp message
    # delivered" (decisions.md §4 still blocks that). See
    # app/agent/notifications.py.
    notification_status: Mapped[str] = mapped_column(String(20), default="not_sent", nullable=False)
    # not_sent | generated | blocked_by_policy

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
