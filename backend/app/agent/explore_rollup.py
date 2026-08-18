"""
Phase 8 Explore rollup — populates `explore_claim_clusters`
(app/models/explore_claim_cluster.py) from recently-completed analyses.

**Why a rollup, not a live query over `analyses`/`claims`**: docs/api-design.md's
"Structural privacy enforcement" requires `/explore/*` to be structurally,
not just conventionally, incapable of joining back to per-user data —
`explore_claim_clusters` has no FK to `users` or `analyses` at all. A live
query path would need to either (a) join to `analyses` and filter
`privacy_mode_snapshot`/`user_id` at read time — exactly the query path the
design explicitly forbids — or (b) duplicate that filtering logic in two
places. A periodic write-once/upsert rollup is the only shape that actually
satisfies "structurally cannot join to users," so this reuses the SAME
in-process asyncio periodic task pattern as retention purge and scheduled
checks (main.py's lifespan) — not a new pipeline: it does not extract
claims, call an LLM, or search evidence. It only reads ALREADY-COMPUTED
`claims.result`/`claim_confidence` and aggregates them.

**Privacy filtering, matching database-schema.md's Privacy Mode table
exactly**: only claims from analyses where `privacy_mode_snapshot IS FALSE`
are ever read — a Privacy-Mode-ON user's claims never reach this table,
full stop (decisions.md §8/database-schema.md: "Explore | Excluded
entirely — never increments explore_claim_clusters").

**Clustering key** reuses `app.agent.duplicate_detection.compute_fingerprint`
(SHA-256 of normalized claim text) — the exact function that module's own
docstring already earmarked for this purpose, not a new fingerprinting
scheme.

**Recompute, not increment**: each tick fully recomputes stats (check_count,
average confidence, most-recent status) for any cluster with at least one
contributing claim inside the lookback window, from a fresh GROUP-BY-style
scan — never an incrementing counter. This makes the rollup naturally
idempotent (a bug or an extra tick can never double-count) at the cost of
only tracking "recent" activity — a cluster whose claims all fall outside
the lookback window simply stops being updated, which is the correct
behavior for a "trending/frequently checked" surface, not a permanent
archive.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.duplicate_detection import compute_fingerprint
from app.core.config import Settings
from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.explore_claim_cluster import ExploreClaimCluster

# decisions.md §1A's ResultLabel vocabulary, most-serious-first — used only
# to pick a single "representative" status when a cluster's claims within
# the window disagree (rare, since fingerprinting already groups
# near-identical claim text); does not change any individual claim's own
# stored result, only which one this rollup surfaces as the cluster's
# headline status.
_STATUS_PRIORITY = [
    "false",
    "misleading",
    "partially_true",
    "unverified",
    "insufficient_evidence",
    "verified",
    "opinion",
    "satire",
]


@dataclass
class _ClaimSample:
    claim_text: str
    category: str | None
    language: str
    result: str
    claim_confidence: float | None
    evidence_count: int
    created_at: datetime


async def _fetch_candidate_claims(session: AsyncSession, cutoff: datetime) -> list[_ClaimSample]:
    from app.models.evidence import Evidence  # local import avoids a module-level cycle risk

    result = await session.execute(
        select(Claim, Analysis.language, Analysis.created_at)
        .join(Analysis, Claim.analysis_id == Analysis.id)
        .where(
            Analysis.privacy_mode_snapshot.is_(False),
            Analysis.is_deleted.is_(False),
            Analysis.status == "completed",
            Analysis.created_at >= cutoff,
            Claim.investigation_complete.is_(True),
        )
    )
    rows = result.all()

    samples: list[_ClaimSample] = []
    for claim, language, analysis_created_at in rows:
        evidence_count_result = await session.execute(
            select(Evidence.id).where(Evidence.claim_id == claim.id)
        )
        evidence_count = len(evidence_count_result.all())
        samples.append(
            _ClaimSample(
                claim_text=claim.claim_text,
                category=claim.category,
                language=language,
                result=claim.result,
                claim_confidence=float(claim.claim_confidence) if claim.claim_confidence is not None else None,
                evidence_count=evidence_count,
                created_at=analysis_created_at,
            )
        )
    return samples


def _pick_representative_status(results: list[str]) -> str:
    present = set(results)
    for candidate in _STATUS_PRIORITY:
        if candidate in present:
            return candidate
    return results[0]


async def run_explore_rollup(session: AsyncSession, settings: Settings, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.explore_rollup_lookback_hours)

    samples = await _fetch_candidate_claims(session, cutoff)

    clusters: dict[str, list[_ClaimSample]] = defaultdict(list)
    for sample in samples:
        fingerprint = compute_fingerprint(sample.claim_text)
        clusters[fingerprint].append(sample)

    for fingerprint, members in clusters.items():
        confidences = [m.claim_confidence for m in members if m.claim_confidence is not None]
        avg_confidence = sum(confidences) / len(confidences) if confidences else None
        # Longest claim text is used as the representative wording — a
        # reasonable, deterministic tie-break (more likely to be the fuller,
        # less-truncated phrasing) without needing an LLM call to pick one.
        representative = max(members, key=lambda m: len(m.claim_text))
        latest = max(members, key=lambda m: m.created_at)

        existing_result = await session.execute(
            select(ExploreClaimCluster).where(ExploreClaimCluster.cluster_fingerprint == fingerprint)
        )
        cluster = existing_result.scalar_one_or_none()

        if cluster is None:
            cluster = ExploreClaimCluster(
                cluster_fingerprint=fingerprint,
                first_seen_at=min(m.created_at for m in members),
                last_seen_at=latest.created_at,
            )
            session.add(cluster)

        cluster.representative_claim_text = representative.claim_text
        cluster.category = latest.category
        cluster.language = latest.language
        cluster.credibility_status = _pick_representative_status([m.result for m in members])
        cluster.credibility_score = avg_confidence
        cluster.source_count = max((m.evidence_count for m in members), default=0)
        cluster.check_count = len(members)
        cluster.last_seen_at = max(cluster.last_seen_at, latest.created_at)

    await session.commit()
    return len(clusters)
