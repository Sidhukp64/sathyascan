"""
safety_gate_events — docs/database-schema.md, decisions.md §6.

Deliberately minimal. Records that a check happened and its outcome —
NEVER the flagged content itself or a description of it. Access to this
table should be more restricted than any other, per database-schema.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SafetyGateEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "safety_gate_events"

    media_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_attachments.id", ondelete="CASCADE"), nullable=True
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    check_type: Mapped[str] = mapped_column(String(30), nullable=False)  # csam_hash_match | content_classifier | other
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # passed | blocked | error
    provider_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # vendor's own case reference — NEVER content
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
