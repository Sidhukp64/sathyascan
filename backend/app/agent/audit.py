"""
Audit-log writer (decisions.md §9: audit_log is "a required, not optional,
MVP component"). Referenced since app/models/audit_log.py was migrated in
Phase 6's DB work; this is its first actual writer.

Deliberately minimal, matching safety_gate_events' precedent (Phase 3):
records STRUCTURAL facts only (who/what/when), never content. `metadata` is
defensively checked against a forbidden-key list at write time — belt and
braces on top of "never pass sensitive data in" being a code-review
discipline, not just a convention, since a future caller adding a field here
is an easy mistake to make silently.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog

# Keys that must never appear in audit metadata, regardless of caller intent.
# Covers OTP codes, JWTs, passwords/hashes, provider secrets, and phone
# numbers/hashes (identify by user_id only, per decisions.md §7/§9).
FORBIDDEN_METADATA_KEYS = {
    "otp_code",
    "code",
    "code_hash",
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "jwt",
    "jwt_secret",
    "secret",
    "api_key",
    "phone_number",
    "phone",
    "phone_hash",
    "phone_number_hash",
    "phone_number_encrypted",
}


class AuditMetadataLeakError(ValueError):
    """Raised when a caller attempts to write a forbidden key into audit
    metadata — fails loud rather than silently stripping the field, so the
    bug is caught in development/tests, not discovered later in a DB dump."""


async def write_audit_log(
    session: AsyncSession,
    *,
    actor_type: str,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    if metadata:
        leaked = FORBIDDEN_METADATA_KEYS & set(metadata.keys())
        if leaked:
            raise AuditMetadataLeakError(f"audit metadata must never include: {sorted(leaked)}")

    entry = AuditLog(
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        audit_metadata=metadata,
    )
    session.add(entry)
    await session.flush()  # assigns entry.id without ending the caller's transaction
    return entry
