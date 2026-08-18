"""
notifications — Phase 8 notification infrastructure.

Channel-abstract by design (`channel` column) so an in-app/future-frontend
channel and a WhatsApp channel share one table/API shape — per the user's
explicit instruction that the abstraction must not need redesigning once
real WhatsApp proactive sending is unblocked. `channel='whatsapp'` rows are
still created and queryable today; only the ACTUAL Meta API call is
withheld (see app/agent/notifications.py).

Deliberately minimal, same "structural facts only" precedent as
audit_log/safety_gate_events: `body` here is short, user-facing notification
text (already rendered in the user's language at write time, mirroring how
url_safety_scans.recommendation is rendered once at persist time) — never
raw claim/evidence content or anything reconstructible into a full
analysis; the notification links back to the entity via
`related_entity_type`/`related_entity_id` for the API to fetch full detail
through the EXISTING History API, not by duplicating it here.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class Notification(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    notification_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # scheduled_check_completed | credibility_changed | scheduled_check_failed | security_event

    channel: Mapped[str] = mapped_column(String(20), default="in_app", nullable=False)
    # in_app | whatsapp — see module docstring: 'whatsapp' rows are created,
    # never actually delivered by Meta's API in this build (decisions.md §4).

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    related_entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 'analysis' | 'scheduled_check' | None (account/security events)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    delivery_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # pending | delivered (in_app: created=delivered) | blocked_by_policy | failed

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
