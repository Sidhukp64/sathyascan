"""
audit_log — docs/database-schema.md, decisions.md §9.

decisions.md §9 explicitly requires this table as "a required, not optional,
MVP component" — designed since the original architecture pass but never
migrated in any of Phases 1-5, since none of them introduced a surface
sensitive enough to need it. Phase 6's dashboard/auth surface is the first
that does: OTP requests/verifications, JWT issuance, privacy-mode changes,
and history deletions are all recorded here (see app/agent/audit.py).

Deliberately minimal, matching safety_gate_events' precedent (Phase 3):
`metadata` NEVER contains message text, transcript content, evidence
snippets, or any other sensitive payload — only structural facts (which
analysis_id, what action, when). Never the phone number or its hash either;
identify by `user_id` only.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class AuditLog(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_log"

    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'user' | 'system' | 'admin'
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. 'otp_requested', 'privacy_mode_changed'
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. 'user', 'analysis'
    entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # Python attribute is `audit_metadata` (SQLAlchemy's declarative Base
    # reserves the bare name `metadata` for Base.metadata) — the actual DB
    # column is named `metadata`, matching database-schema.md exactly.
    audit_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
