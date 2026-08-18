"""
admin_users — Phase 9 (roadmap §9.1: "Admin System"). A SEPARATE identity
table from `users` (WhatsApp end users), matching the original architecture
sketch (docs/database-schema.md's "Ops / compliance" section) — admin
operators authenticate with email+password, not WhatsApp-OTP, since they
are internal staff, not reachable via a phone number necessarily, and
mixing "admin" authority into the same table that represents end-user
identity would blur the "role-gated ops" boundary api-design.md's original
route-group list already called for.

**No public signup endpoint exists anywhere for this table** — an
admin_users row is created only via `scripts/create_admin.py`, a local CLI
run by someone with direct database access, never over HTTP. A public
"create admin" API would itself be the single largest privilege-escalation
attack surface this phase could introduce.

`role` is a plain string, not an enum column (SQLite has no native enum;
matches every other status/role field in this codebase — `analyses.status`,
`scheduled_checks.status`, etc. are all `String`, validated in Python).
Two values are meaningful today: `"admin"` (full access to every
`/api/v1/admin/*` route) and `"moderator"` (appeals/moderation review only —
see `app/api/v1/deps.py`'s `require_admin_role`/
`require_moderator_or_admin_role`).

`password_hash` is a real bcrypt hash (`app/core/admin_security.py`) — NOT
the keyed-HMAC pattern `hash_phone_number`/`hash_otp_code` use elsewhere in
this codebase. Phone numbers and OTP codes are not user-chosen secrets (HMAC
with a server-side pepper is the right tool for a lookup key); a password
IS a user-chosen, low-entropy secret, which specifically needs a slow,
salted KDF like bcrypt to resist offline brute-forcing of a stolen hash —
using the wrong tool here would be a real security regression, not a style
choice.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class AdminUser(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "admin_users"

    # Stored lowercased at write time (app/core/admin_security.py) so the
    # UNIQUE constraint and lookup are case-insensitive without depending on
    # a citext extension (SQLite/plain Postgres don't have one by default —
    # the original database-schema.md sketch's "citext" was aspirational).
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="admin", nullable=False)
    # "admin" | "moderator"

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
