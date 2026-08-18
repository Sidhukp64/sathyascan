"""
analyses — docs/database-schema.md, decisions.md §1/§1A/§8/§12.

Adds columns beyond the originally documented schema:
`content_fingerprint` — needed for duplicate-content detection (decisions.md
§12), not previously specified.
`session_id` (Phase 8) — nullable FK to `analysis_sessions`, the NEW
"group related analyses" feature (see app/models/analysis_session.py's
docstring for why it's a separate table name from the already-documented,
different-purpose `conversation_sessions`). An analysis not yet assigned to
any session is simply NULL here — the existing History API is completely
unaffected by this column's existence (it never filters or joins on it
unless a caller explicitly asks via the new session endpoints).
docs/database-schema.md has been updated to match.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class Analysis(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "analyses"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    input_type: Mapped[str] = mapped_column(String(20), default="text", nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)

    # SHA-256 hex of the normalized claim text — duplicate-detection key
    # (new in Phase 2, see module docstring).
    content_fingerprint: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    source_wamid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # pending | processing | completed | failed

    overall_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # verified | false | misleading | partially_true | unverified | opinion |
    # satire | insufficient_evidence | no_claims_detected

    language: Mapped[str] = mapped_column(String(5), nullable=False)

    # Captured at analysis time, immutable even if the user later flips the
    # setting (decisions.md §8).
    privacy_mode_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Budget guard counters (decisions.md §1).
    evidence_search_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    llm_tool_call_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    budget_limit_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    reused_from_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), nullable=True
    )

    # Phase 8 — see module docstring. SET NULL (not CASCADE): deleting a
    # session must never delete the analyses in it, only ungroup them.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
