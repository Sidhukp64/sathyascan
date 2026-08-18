"""
claims — docs/database-schema.md, decisions.md §1A/§3/§5.

`result`, `investigation_complete`, `incomplete_reason` implement the
UNVERIFIED vs INSUFFICIENT_EVIDENCE distinction exactly as decisions.md §1A
defines it — see app/agent/classification.py for the enforcement logic.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class Claim(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "claims"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    result: Mapped[str] = mapped_column(String(30), nullable=False)
    # verified | false | misleading | partially_true | unverified | opinion |
    # satire | insufficient_evidence  (decisions.md §1A)

    # investigation_complete = False  <=>  result = 'insufficient_evidence'
    # (invariant enforced in app/agent/classification.py, not just DB comment)
    investigation_complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    incomplete_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # search_limit | tool_call_limit | provider_unavailable | timeout | infra_error | other

    claim_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    media_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)  # unused in Phase 2
    evidence_strength: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    reasoning_text: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # gov_scheme | elections | health | tech | ai_media | scam | other

    evidence_tier_met: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 1 / 2 / 3 / None

    # True when the LLM's suggested classification was overridden by the
    # deterministic evidence-tier guard (app/agent/classification.py). Kept
    # for audit/appeals review, per the "record why" principle.
    policy_override_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
