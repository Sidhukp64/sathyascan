"""
users — docs/database-schema.md, decisions.md §7.

Phase 2 trimmed the full target schema's `users` table to what the text
pipeline actually needed. Phase 6 adds `dashboard_accounts`/
`otp_verifications` (app/models/dashboard_account.py,
app/models/otp_verification.py) as separate tables referencing `users.id` —
this table itself is unchanged; dashboard identity is layered on top of the
existing WhatsApp-identified user, not merged into this row.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    # AES-256-GCM encrypted (app.core.encryption) — needed to send WhatsApp
    # replies. Never returned by any API response (decisions.md §7).
    phone_number_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Keyed HMAC-SHA256(phone, PHONE_HASH_PEPPER) — NOT a bare hash
    # (decisions.md §7). Used for lookup; safe to log (truncated).
    phone_number_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    preferred_language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    auto_detect_language: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Privacy Mode ON by default (decisions.md §8).
    privacy_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Phase 9 — admin "Suspend user" (roadmap §9.1). Deliberately a separate
    # flag from is_deleted: a suspension is reversible (Reactivate), an
    # account deletion is not. Enforced in TWO places, both reusing existing
    # choke points rather than new middleware: app/api/v1/deps.py's
    # get_current_user (dashboard JWT auth — same spot is_deleted is already
    # checked) and app/agent/user_service.py's get_or_create_user callers in
    # app/webhook/whatsapp/router.py (WhatsApp messages get a distinct,
    # honest decline reply instead of being silently dropped or processed).
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
