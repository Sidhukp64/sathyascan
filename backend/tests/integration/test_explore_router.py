"""
Integration tests for the Phase 8 Explore API (app/api/v1/routers/explore.py)
— full HTTP round trips using tests/conftest.py's shared `auth_env` fixture.
No auth is used anywhere here — proving these routes work WITHOUT a Bearer
token is itself the point (docs/api-design.md: "Explore — PUBLIC... no auth
required").
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.explore_claim_cluster import ExploreClaimCluster


async def _seed_cluster(sessionmaker, **overrides) -> uuid.UUID:
    async with sessionmaker() as session:
        now = datetime.now(timezone.utc)
        cluster = ExploreClaimCluster(
            cluster_fingerprint=uuid.uuid4().hex,
            representative_claim_text=overrides.get("representative_claim_text", "A public claim"),
            category=overrides.get("category", "health"),
            language=overrides.get("language", "en"),
            credibility_status=overrides.get("credibility_status", "false"),
            credibility_score=overrides.get("credibility_score", 0.8),
            source_count=overrides.get("source_count", 2),
            check_count=overrides.get("check_count", 5),
            first_seen_at=overrides.get("first_seen_at", now - timedelta(days=1)),
            last_seen_at=overrides.get("last_seen_at", now),
        )
        session.add(cluster)
        await session.commit()
        await session.refresh(cluster)
        return cluster.id


@pytest.mark.asyncio
async def test_list_claims_works_without_authentication(auth_env):
    await _seed_cluster(auth_env.sessionmaker)
    resp = await auth_env.client.get("/api/v1/explore/claims")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_response_never_contains_a_phone_number_shaped_field(auth_env):
    await _seed_cluster(auth_env.sessionmaker)
    resp = await auth_env.client.get("/api/v1/explore/claims")
    body_text = resp.text
    for forbidden in ("phone_number", "phone_hash", "user_id"):
        assert forbidden not in body_text


@pytest.mark.asyncio
async def test_trending_sort_orders_by_check_count(auth_env):
    await _seed_cluster(auth_env.sessionmaker, representative_claim_text="less checked", check_count=1)
    await _seed_cluster(auth_env.sessionmaker, representative_claim_text="most checked", check_count=99)

    resp = await auth_env.client.get("/api/v1/explore/claims", params={"sort": "trending"})
    items = resp.json()["items"]
    assert items[0]["representative_claim_text"] == "most checked"


@pytest.mark.asyncio
async def test_recent_sort_orders_by_last_seen(auth_env):
    now = datetime.now(timezone.utc)
    await _seed_cluster(auth_env.sessionmaker, representative_claim_text="older", last_seen_at=now - timedelta(days=5))
    await _seed_cluster(auth_env.sessionmaker, representative_claim_text="newer", last_seen_at=now)

    resp = await auth_env.client.get("/api/v1/explore/claims", params={"sort": "recent"})
    items = resp.json()["items"]
    assert items[0]["representative_claim_text"] == "newer"


@pytest.mark.asyncio
async def test_invalid_sort_value_rejected(auth_env):
    resp = await auth_env.client.get("/api/v1/explore/claims", params={"sort": "not-a-real-sort"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_filter_by_category(auth_env):
    await _seed_cluster(auth_env.sessionmaker, category="health")
    await _seed_cluster(auth_env.sessionmaker, category="elections")

    resp = await auth_env.client.get("/api/v1/explore/claims", params={"category": "elections"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["category"] == "elections"


@pytest.mark.asyncio
async def test_filter_by_credibility_status(auth_env):
    await _seed_cluster(auth_env.sessionmaker, credibility_status="false")
    await _seed_cluster(auth_env.sessionmaker, credibility_status="verified")

    resp = await auth_env.client.get("/api/v1/explore/claims", params={"credibility_status": "verified"})
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_search_query(auth_env):
    await _seed_cluster(auth_env.sessionmaker, representative_claim_text="Vaccines cause microchips")
    await _seed_cluster(auth_env.sessionmaker, representative_claim_text="The election was rigged")

    resp = await auth_env.client.get("/api/v1/explore/claims", params={"q": "vaccine"})
    body = resp.json()
    assert body["total"] == 1
    assert "Vaccines" in body["items"][0]["representative_claim_text"]


@pytest.mark.asyncio
async def test_pagination(auth_env):
    for i in range(5):
        await _seed_cluster(auth_env.sessionmaker, representative_claim_text=f"claim {i}")

    resp = await auth_env.client.get("/api/v1/explore/claims", params={"page": 1, "page_size": 2})
    body = resp.json()
    assert body["total"] == 5
    assert body["total_pages"] == 3
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_get_single_claim_cluster(auth_env):
    cluster_id = await _seed_cluster(auth_env.sessionmaker)
    resp = await auth_env.client.get(f"/api/v1/explore/claims/{cluster_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(cluster_id)


@pytest.mark.asyncio
async def test_get_nonexistent_claim_cluster_returns_404(auth_env):
    resp = await auth_env.client.get(f"/api/v1/explore/claims/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_categories_endpoint_returns_counts(auth_env):
    await _seed_cluster(auth_env.sessionmaker, category="health")
    await _seed_cluster(auth_env.sessionmaker, category="health")
    await _seed_cluster(auth_env.sessionmaker, category="elections")

    resp = await auth_env.client.get("/api/v1/explore/categories")
    body = resp.json()
    counts = {c["category"]: c["count"] for c in body["categories"]}
    assert counts["health"] == 2
    assert counts["elections"] == 1


@pytest.mark.asyncio
async def test_explore_rate_limit_trips(auth_env):
    auth_env.settings.explore_rate_limit_per_ip_per_minute = 2
    statuses = []
    for _ in range(4):
        resp = await auth_env.client.get("/api/v1/explore/claims")
        statuses.append(resp.status_code)
    assert statuses.count(200) == 2
    assert statuses.count(429) == 2
