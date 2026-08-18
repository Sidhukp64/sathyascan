"""
Declarative base + shared column helpers for all Phase 2 models.

Phase 2 scope note (docs/decisions.md, docs/database-schema.md): only the
subset of the full target schema actually needed by the text fact-checking
pipeline is modeled here — users, analyses, claims, evidence,
source_credibility_registry, usage_ledger. Dashboard/media/URL-safety/
scheduling tables from database-schema.md are intentionally not modeled yet.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
