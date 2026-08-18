"""
media_forensics_results — docs/database-schema.md, decisions.md §2.

Phase 3 note: this table stays EMPTY in this build — no real image-forensics
provider is wired (NullImageForensicsProvider always reports UNAVAILABLE),
and `provider_name`/`model_version` are NOT NULL because decisions.md §2
treats them as mandatory, user-facing fields whenever a real result exists.
Rather than write a row with placeholder values for a check that didn't
really happen, the absence of a row IS the signal "no forensics attempted"
— clean, and requires no special-casing once a real provider is added later.
"""

import uuid

from sqlalchemy import JSON, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaForensicsResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "media_forensics_results"

    media_attachment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_attachments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)  # image_analyzer | video_deepfake | audio_voice_detect
    provider_name: Mapped[str] = mapped_column(String(50), nullable=False)  # MANDATORY, user-facing (decisions.md §2)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)  # MANDATORY, user-facing

    ai_generated_probability: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    manipulation_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    raw_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
