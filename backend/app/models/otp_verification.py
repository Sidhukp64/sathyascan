"""
otp_verifications — docs/database-schema.md, api-design.md.

Migrated exactly as designed. `code_hash` — never the raw 6-digit code —
via a keyed HMAC (app/core/security.py's `hash_otp_code`, reusing
PHONE_HASH_PEPPER, same pattern as phone-number hashing rather than
inventing a second secret). `attempts` bounds brute-force guessing within
the OTP's short validity window (app/api/v1/auth.py enforces a max).

Phase 6 note (decisions.md §4 conflict, confirmed with the user): OTP
delivery uses the existing free-form WhatsAppSender — this is a genuinely
PROACTIVE message (sent the moment a user starts a dashboard login, not in
reply to an inbound message), which technically requires a pre-approved
Meta message template outside an active 24h conversation window before
this can work in a real production deployment (same precondition
decisions.md §4/§14 already established for "Check This Tomorrow"). Built
and fully tested here per the user's explicit instruction; the production
limitation is documented, not silently ignored.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class OtpVerification(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "otp_verifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # 'whatsapp' | 'email' — only 'whatsapp' used in Phase 6
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)  # 'dashboard_link' | 'login'
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
