"""
media_attachments — docs/database-schema.md.

Phase 3 note: `storage_path` stays permanently NULL — Phase 3 deliberately
does not implement blob storage at all. Media is downloaded to memory,
validated, safety-checked, OCR'd, and discarded within the same background
task; "secure cleanup" holds by construction for the raw bytes (nothing
durable to clean up there). The column is kept for forward-compatibility
with a future phase that needs to persist media (e.g. for appeals review).

Phase 6 adds `retention_expires_at` (already documented in
database-schema.md, never populated until now) — this row itself (plus its
cascaded `transcripts`/`video_frames`/`safety_gate_events`/
`media_forensics_results` children) IS real, durable metadata even though
no blob exists, and Privacy Mode's retention purge (app/agent/retention.py)
needs a real expiry to scan by. Populated at creation time by
Image/Audio/VideoPipeline from the analysis's `privacy_mode_snapshot` +
the configured TTL — see each pipeline's module docstring.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaAttachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "media_attachments"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    whatsapp_media_id: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # always NULL in Phase 3, see docstring
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)  # image | video | audio | document
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)  # the ACTUAL sniffed type, never the claimed one
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
