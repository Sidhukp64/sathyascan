"""
url_safety_scans — docs/database-schema.md, decisions.md §15, Phase 4.

Table design predates Phase 4 (see database-schema.md's Phase 2/3 note
listing it as "design-reference-only, not migrated") — this migrates the
already-agreed schema, unchanged, rather than inventing a new one.

`reasons` stores structured finding data (FindingCode + params from
app/agent/tools/url_safety.py), NOT pre-rendered text — keeps the row
language-neutral even though `recommendation` below is rendered once, in
the analysis's language, at persist time (matches how `analyses.language`
already fixes a single language per analysis).
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class UrlSafetyScan(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "url_safety_scans"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)

    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    # safe | low | medium | high | critical

    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    threat_intel_matches: Mapped[list | None] = mapped_column(JSON, nullable=True)

    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
