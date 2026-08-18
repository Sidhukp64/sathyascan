"""
source_credibility_registry — docs/database-schema.md, decisions.md §3.

The ONLY source of evidence credibility anywhere in the pipeline. A domain
absent from this table defaults to tier_3_other — never inferred from search
rank (decisions.md §3).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow

TIER_1_GOV_OFFICIAL = "tier_1_gov_official"
TIER_1_PRIMARY_DOC = "tier_1_primary_doc"
TIER_1_FACTCHECK_ORG = "tier_1_factcheck_org"
TIER_2_REPUTABLE_NEWS = "tier_2_reputable_news"
TIER_2_RESEARCH = "tier_2_research"
TIER_3_OTHER = "tier_3_other"
LOW_TRUST = "low_trust"
BLACKLISTED = "blacklisted"

TIER_1_VALUES = {TIER_1_GOV_OFFICIAL, TIER_1_PRIMARY_DOC, TIER_1_FACTCHECK_ORG}
TIER_2_VALUES = {TIER_2_REPUTABLE_NEWS, TIER_2_RESEARCH}


class SourceCredibilityRegistry(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "source_credibility_registry"

    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    credibility_tier: Mapped[str] = mapped_column(String(30), nullable=False)
    region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def tier_rank(credibility_tier: str | None) -> int | None:
    """Maps a credibility_tier string to a numeric rank (1=best), matching
    claims.evidence_tier_met's 1/2/3 convention. None/unranked -> None."""
    if credibility_tier in TIER_1_VALUES:
        return 1
    if credibility_tier in TIER_2_VALUES:
        return 2
    if credibility_tier == TIER_3_OTHER:
        return 3
    return None  # low_trust / blacklisted / unknown — never counts toward a tier bar
