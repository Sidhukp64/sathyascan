"""
appeals — Phase 9 (roadmap §9.2), matching the original design sketch in
docs/database-schema.md's "Ops / compliance" section, extended with a
`resolved_by` admin reference for accountability (not in the original
sketch — added because "who decided this" is exactly the kind of fact
decisions.md §9's audit-trail principle exists to preserve).

**Users can never directly modify a fact-check result through this table.**
An appeal is a request for HUMAN review, recorded as a distinct row — it
never writes to `analyses`/`claims` itself. Approving an appeal is an admin
decision recorded here (`status`, `admin_notes`, `resolved_by`); actually
changing a stored verdict, if ever warranted, is a deliberate follow-up
action outside this table's scope, not an automatic side effect of
`status='approved'` — building an automatic-reclassification pipeline was
not requested and would be exactly the kind of unrequested, ambiguous new
feature the user's Phase 9 instructions rule out.

`analysis_id` is CASCADE (unlike `scheduled_checks.source_analysis_id`,
which is deliberately SET NULL so a re-check can outlive its source): an
appeal has no purpose once the analysis it's about no longer exists, so it
is explicitly deleted alongside the analysis in
`app/agent/retention.py::delete_analysis_and_children` — same "explicit
Python delete, never trust the DB's ON DELETE action" discipline every
other table in this codebase already follows.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class Appeal(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "appeals"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("claims.id", ondelete="SET NULL"), nullable=True, index=True
    )

    reason_text: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    # open | under_review | approved | rejected | escalated | cancelled

    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
