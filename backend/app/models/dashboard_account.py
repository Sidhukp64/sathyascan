"""
dashboard_accounts — docs/database-schema.md, api-design.md.

Migrated exactly as designed: `email`/`password_hash` stay nullable —
Phase 6 is WhatsApp-OTP-only (api-design.md's confirmed decision), no
password auth built. One row per `users.id` (UNIQUE), created on first
successful OTP verification (app/api/v1/auth.py).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class DashboardAccount(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "dashboard_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )

    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # unused in Phase 6 — OTP-only
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
