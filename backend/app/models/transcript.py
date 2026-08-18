"""
transcripts — docs/database-schema.md, decisions.md §11, Phase 5.

Table design predates Phase 5 (listed in database-schema.md as "design-
reference-only, not migrated") — this migrates the already-agreed schema,
unchanged. Generic across BOTH audio-clip transcripts (AudioPipeline) and
video-extracted-audio transcripts (VideoPipeline, Phase 5b) — no
modality-specific columns needed, since `media_attachment_id` already
disambiguates via `media_attachments.media_type`.

Stays EMPTY of any "engine" value beyond NullSpeechToTextProvider's until a
real, benchmarked STT engine is wired (decisions.md §11's sequencing rule,
same precedent as media_forensics_results in Phase 3).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class Transcript(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "transcripts"

    media_attachment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_attachments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(50), nullable=True)  # whisper | indicwhisper | ...

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
