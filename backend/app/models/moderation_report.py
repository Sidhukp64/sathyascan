"""
moderation_reports — Phase 9 (roadmap §9.3: "Moderation"). A ticketing/
review table for human admins, NOT a new AI content-moderation system —
per the user's explicit instruction: "Do not build unnecessary new AI
moderation systems if existing infrastructure already provides the
required capability." Detection/prevention infrastructure already exists
(`app/agent/safety_gate.py`'s non-bypassable pre-analysis gate,
`app/agent/tools/url_safety.py`'s heuristics) — this table is the
downstream "a human flagged this for review" record those systems don't
cover: user-submitted reports about content that already passed through
the pipeline.

Three distinct target shapes, all optional, matching the different things a
report can be about:
  - `target_analysis_id` — a specific analysis the reporter owns or saw
    (e.g. via a shared link); SET NULL if the analysis is later purged/
    deleted, same "the report itself still has value even after its
    subject is gone" reasoning as scheduled_checks' source_analysis_id.
  - `target_explore_cluster_id` — a public Explore item (which has NO
    per-user linkage at all — see explore_claim_clusters' own docstring —
    so a report against Explore content can only ever reference the
    cluster, never a user's specific analysis).
  - `target_url` — a freeform URL (e.g. "this evidence source itself looks
    malicious"), not necessarily tied to any one analysis.
Exactly one of these three should be populated for a given `target_type`;
enforced at the API layer (app/schemas/moderation.py), not a DB constraint
(SQLite has no native CHECK-with-XOR-across-columns convenience, and this
codebase's precedent — e.g. scheduled_checks' category — is to validate
optional/exclusive fields in the schema layer, not the DB layer).

`reporter_user_id` is CASCADE — deleted alongside the rest of a user's owned
rows in `app/agent/account_deletion.py`, same as every other user-owned
table in this codebase.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class ModerationReport(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "moderation_reports"

    reporter_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    report_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # spam | abuse | suspicious_content | malicious_url | other

    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # analysis | url | explore_claim | other
    target_analysis_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_explore_cluster_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("explore_claim_clusters.id", ondelete="SET NULL"), nullable=True
    )
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    description: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    # open | reviewed | actioned | dismissed

    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
