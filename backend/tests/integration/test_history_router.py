"""
Integration tests for the Phase 6 History API (app/api/v1/routers/history.py)
— full HTTP round trips using tests/conftest.py's shared `auth_env` fixture,
same pattern as test_auth_router.py. Analyses/Claims/Evidence are seeded
directly via the sessionmaker (this router surfaces already-computed
results; it doesn't run the fact-checking pipeline itself, so there's no
pipeline to drive through Depends() overrides here).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.claim import Claim
from app.models.evidence import Evidence
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812345101"
PHONE_B = "919812345102"


async def _seed_analysis(
    sessionmaker,
    user_id: uuid.UUID,
    *,
    input_type: str = "text",
    overall_result: str = "false",
    status: str = "completed",
    created_at: datetime | None = None,
    with_claim: bool = True,
    language: str = "en",
) -> uuid.UUID:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type=input_type,
            input_text="The government announced a new scheme today.",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status=status,
            overall_result=overall_result,
            language=language,
            privacy_mode_snapshot=True,
            created_at=created_at or datetime.now(timezone.utc),
            completed_at=created_at or datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.flush()

        if with_claim:
            claim = Claim(
                analysis_id=analysis.id,
                claim_text="The government announced a new scheme today.",
                claim_order=0,
                result=overall_result,
                investigation_complete=True,
                claim_confidence=0.9,
                evidence_strength=0.8,
                evidence_tier_met=1,
                reasoning_text="No official source confirms this.",
                category="gov_scheme",
            )
            session.add(claim)
            await session.flush()

            session.add(
                Evidence(
                    claim_id=claim.id,
                    source_url="https://pib.gov.in/example",
                    source_domain="pib.gov.in",
                    source_title="Official statement",
                    publisher_name="PIB",
                    stance="contradicting",
                    credibility_tier="tier_1_gov_official",
                    snippet_text="No such scheme has been announced.",
                    retrieved_at=datetime.now(timezone.utc),
                )
            )

        await session.commit()
        return analysis.id


async def _get_user_id_by_phone_hash_lookup(sessionmaker) -> uuid.UUID:
    """Test helper: after a login, exactly one new user row exists per
    distinct phone number used so far in the test — returns the most
    recently created one."""
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


@pytest.mark.asyncio
async def test_history_list_requires_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_history_list_empty_for_new_user(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["total_pages"] == 0


@pytest.mark.asyncio
async def test_history_list_returns_own_analyses(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id)
    await _seed_analysis(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


@pytest.mark.asyncio
async def test_history_list_excludes_soft_deleted(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(Analysis).where(Analysis.id == analysis_id))
        row = result.scalar_one()
        row.is_deleted = True
        await session.commit()

    resp = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_history_list_user_a_cannot_see_user_b_analyses(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    user_b_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    assert user_a_id != user_b_id

    await _seed_analysis(auth_env.sessionmaker, user_a_id)
    await _seed_analysis(auth_env.sessionmaker, user_b_id)
    await _seed_analysis(auth_env.sessionmaker, user_b_id)

    resp_a = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token_a}"})
    resp_b = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_a.json()["total"] == 1
    assert resp_b.json()["total"] == 2


@pytest.mark.asyncio
async def test_history_list_pagination(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    for _ in range(5):
        await _seed_analysis(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.get(
        "/api/v1/history", params={"page": 1, "page_size": 2}, headers={"Authorization": f"Bearer {token}"}
    )
    body = resp.json()
    assert body["total"] == 5
    assert body["total_pages"] == 3
    assert len(body["items"]) == 2

    resp_page3 = await auth_env.client.get(
        "/api/v1/history", params={"page": 3, "page_size": 2}, headers={"Authorization": f"Bearer {token}"}
    )
    assert len(resp_page3.json()["items"]) == 1


@pytest.mark.asyncio
async def test_history_list_filters_by_type_and_result(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id, input_type="text", overall_result="false")
    await _seed_analysis(auth_env.sessionmaker, user_id, input_type="image", overall_result="verified")

    resp = await auth_env.client.get(
        "/api/v1/history", params={"type": "image"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["input_type"] == "image"

    resp2 = await auth_env.client.get(
        "/api/v1/history", params={"result": "verified"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp2.json()["total"] == 1
    assert resp2.json()["items"][0]["overall_result"] == "verified"


@pytest.mark.asyncio
async def test_history_list_filter_by_date_range(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    recent = datetime.now(timezone.utc)
    await _seed_analysis(auth_env.sessionmaker, user_id, created_at=old)
    await _seed_analysis(auth_env.sessionmaker, user_id, created_at=recent)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    resp = await auth_env.client.get(
        "/api/v1/history", params={"from": cutoff}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_history_list_query_search(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id)

    resp_match = await auth_env.client.get(
        "/api/v1/history", params={"q": "government"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_match.json()["total"] == 1

    resp_no_match = await auth_env.client.get(
        "/api/v1/history", params={"q": "completely-unrelated-xyz"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp_no_match.json()["total"] == 0


@pytest.mark.asyncio
async def test_history_detail_returns_claims_and_evidence(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.get(
        f"/api/v1/history/{analysis_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_id"] == str(analysis_id)
    assert len(body["claims"]) == 1
    assert body["claims"][0]["result"] == "false"
    assert len(body["claims"][0]["evidence"]) == 1
    assert body["claims"][0]["evidence"][0]["domain"] == "pib.gov.in"


@pytest.mark.asyncio
async def test_history_detail_user_b_cannot_read_user_a_analysis(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)

    resp = await auth_env.client.get(
        f"/api/v1/history/{analysis_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_detail_nonexistent_id_returns_404_not_500(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        f"/api/v1/history/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_detail_requires_auth(auth_env):
    resp = await auth_env.client.get(f"/api/v1/history/{uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_single_history_item(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.delete(
        f"/api/v1/history/{analysis_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 1

    follow_up = await auth_env.client.get(
        f"/api/v1/history/{analysis_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert follow_up.status_code == 404


@pytest.mark.asyncio
async def test_delete_single_history_item_cannot_target_other_users_analysis(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    analysis_id = await _seed_analysis(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    resp = await auth_env.client.delete(
        f"/api/v1/history/{analysis_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404

    # Confirm it was NOT actually deleted by B's failed attempt.
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(Analysis).where(Analysis.id == analysis_id))
        row = result.scalar_one()
        assert row.is_deleted is False


@pytest.mark.asyncio
async def test_clear_history_deletes_only_callers_rows(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_a_id)
    await _seed_analysis(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    user_b_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_b_id)

    resp = await auth_env.client.delete("/api/v1/history", headers={"Authorization": f"Bearer {token_a}"})
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 2

    resp_a_list = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token_a}"})
    assert resp_a_list.json()["total"] == 0

    resp_b_list = await auth_env.client.get("/api/v1/history", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_b_list.json()["total"] == 1  # untouched


@pytest.mark.asyncio
async def test_history_list_filter_by_language(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    await _seed_analysis(auth_env.sessionmaker, user_id, language="en")
    await _seed_analysis(auth_env.sessionmaker, user_id, language="ml")

    resp = await auth_env.client.get(
        "/api/v1/history", params={"language": "ml"}, headers={"Authorization": f"Bearer {token}"}
    )
    body = resp.json()
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_history_list_sort_oldest_first(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id_by_phone_hash_lookup(auth_env.sessionmaker)
    older = datetime.now(timezone.utc) - timedelta(days=2)
    newer = datetime.now(timezone.utc)
    await _seed_analysis(auth_env.sessionmaker, user_id, created_at=older)
    await _seed_analysis(auth_env.sessionmaker, user_id, created_at=newer)

    resp = await auth_env.client.get(
        "/api/v1/history", params={"sort": "oldest"}, headers={"Authorization": f"Bearer {token}"}
    )
    items = resp.json()["items"]
    assert items[0]["created_at"] <= items[1]["created_at"]


@pytest.mark.asyncio
async def test_history_list_invalid_sort_rejected(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/history", params={"sort": "not-a-sort"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 400
