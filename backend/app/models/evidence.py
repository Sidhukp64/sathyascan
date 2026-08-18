"""
evidence — docs/database-schema.md, decisions.md §3.

Snippet-only storage (no full-text scraping/copyright risk); credibility
comes only from source_credibility_registry, never from search rank.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class Evidence(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "evidence"

    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True)

    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    stance: Mapped[str] = mapped_column(String(20), nullable=False)
    # supporting | contradicting | neutral | no_reliable_evidence

    relevance_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    credibility_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    credibility_tier: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Short snippet ONLY — never the full article (decisions.md §3).
    snippet_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
