"""
video_frames — new table, Phase 5b (not in the original database-schema.md
design set, which predates any video work — the closest existing precedent
is `transcripts`, mirrored here for OCR-per-frame results instead of a
whole-clip transcript).

Deliberately NO frame image is ever stored — only the extracted TEXT,
timestamp, language, and confidence, consistent with Phase 3's "no blob
storage" precedent (media_attachments.storage_path stays NULL there too).
See app/media/frame_extractor.py's module docstring.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class VideoFrame(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "video_frames"

    media_attachment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_attachments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    frame_timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
