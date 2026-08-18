"""
explore_claim_clusters — Phase 8 Explore (docs/database-schema.md's
original design, docs/api-design.md's "Structural privacy enforcement":
"/overview and /explore/* must never share a query path... implement this
as two physically separate repository/service classes — one that can join
to users and one that structurally cannot").

**No FK to `users` or `analyses` anywhere on this table, by design** — this
is what makes it structurally, not just conventionally, incapable of
leaking per-user data: even a bug in the Explore query path can't join back
to identify who submitted a claim, because there is no column to join on.
Populated by a write-once/upsert rollup (`app/agent/explore_rollup.py`),
never read live from `analyses`/`claims` — see that module's docstring for
why (privacy-mode filtering, "do not create a second fact-checking
pipeline").

`cluster_fingerprint` reuses `app/agent/duplicate_detection.py`'s existing
`compute_fingerprint` (SHA-256 of normalized claim text) as the clustering
key — the exact function that module's own docstring already earmarked for
this purpose ("Cross-user reuse... belongs with the Explore feature... which
isn't built yet"), not a new fingerprinting scheme.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class ExploreClaimCluster(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "explore_claim_clusters"

    cluster_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    representative_claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(5), nullable=False)

    credibility_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # same ResultLabel vocabulary as claims.result (decisions.md §1A) — the
    # MOST RECENT contributing claim's result, not a blended/averaged label.
    credibility_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # average claim_confidence across contributing claims in the current
    # rollup window — never a single claim's raw score presented as if it
    # were the whole cluster's certainty.

    source_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    check_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # how many times a claim matching this fingerprint has been checked —
    # "frequently checked" / "trending" both derive from this + last_seen_at.

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
