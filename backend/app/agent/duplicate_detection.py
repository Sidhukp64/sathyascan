"""
Duplicate-content detection (decisions.md §12).

Scope decision (documented, not silently assumed): Phase 2 only reuses a
PRIOR ANALYSIS BY THE SAME USER. Cross-user reuse (the aggregate "1,245
people already checked this" Explore-style dedup) belongs with the Explore
feature (`explore_claim_clusters`, database-schema.md) which isn't built
yet — building safe cross-user reuse now would mean re-solving Explore's
privacy-separation design early, for a feature that isn't live. Same-user
reuse trivially satisfies "never leak another user's private analysis"
because it's the same user's own data.

Privacy Mode interaction is self-enforcing, not special-cased: a
Privacy-Mode-ON analysis is purged quickly by the (Phase 5) retention jobs;
once purged, its row is simply gone, so it naturally stops being reusable —
no extra check needed here beyond the existing is_deleted filter.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import Analysis

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_for_fingerprint(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, normalize unicode.
    Not the same normalization as parser.py's safety sanitization (control
    chars/length) — this is specifically for matching near-identical claim
    text, so it's more aggressive."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _PUNCTUATION_RE.sub("", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def compute_fingerprint(text: str) -> str:
    normalized = normalize_for_fingerprint(text)
    return sha256(normalized.encode("utf-8")).hexdigest()


async def find_reusable_analysis(
    session: AsyncSession,
    user_id: UUID,
    content_fingerprint: str,
    language: str,
    window_hours: int,
) -> Analysis | None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    result = await session.execute(
        select(Analysis)
        .where(
            Analysis.user_id == user_id,
            Analysis.content_fingerprint == content_fingerprint,
            Analysis.language == language,
            Analysis.status == "completed",
            Analysis.is_deleted.is_(False),
            Analysis.created_at >= cutoff,
        )
        .order_by(Analysis.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
