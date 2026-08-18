"""
analysis_sessions — Phase 8 "Conversation Sessions" (grouping related
analyses in the dashboard, e.g. all the checks from one investigation).

**Naming note, deliberate**: docs/database-schema.md already documents a
DIFFERENT, still-unmigrated `conversation_sessions` table — that one mirrors
short-lived WhatsApp CONVERSATION-FLOW state (last_analysis_id,
pending_prompt, mid-flow language override; Redis is its hot path, Postgres
just an optional durable mirror). This is a genuinely different feature
(user-organized grouping of History items) that happens to share a name in
the Phase 8 roadmap's prose. Named `analysis_sessions` here specifically to
avoid colliding with that already-reserved name/design, not because of any
locked-decision conflict — both can coexist; only this one is built now.

One analysis belongs to at most one session at a time (a single nullable FK
on `analyses`, not a many-to-many join table) — matches the "conversation
thread" mental model the roadmap describes (open a session, add checks to
it) rather than a tagging system.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class AnalysisSession(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "analysis_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
