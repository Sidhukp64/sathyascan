"""
Integration tests for Phase 8 full account deletion
(`DELETE /api/v1/account`, app/api/v1/routers/account.py +
app/agent/account_deletion.py) — full HTTP round trips using
tests/conftest.py's shared `auth_env` fixture. Security/privacy-focused per
the roadmap's explicit "Add security and privacy tests" instruction.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.analysis import Analysis
from app.models.analysis_session import AnalysisSession
from app.models.audit_log import AuditLog
from app.models.claim import Claim
from app.models.dashboard_account import DashboardAccount
from app.models.evidence import Evidence
from app.models.notification import Notification
from app.models.otp_verification import OtpVerification
from app.models.scheduled_check import ScheduledCheck
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812346201"
PHONE_B = "919812346202"


async def _get_user_by_id(sessionmaker, user_id: uuid.UUID) -> User:
    async with sessionmaker() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one()


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


async def _seed_full_account_data(sessionmaker, user_id: uuid.UUID) -> dict:
    async with sessionmaker() as session:
        analysis = Analysis(
            user_id=user_id,
            input_type="text",
            input_text="x",
            content_fingerprint=uuid.uuid4().hex,
            source_wamid=f"wamid.{uuid.uuid4().hex}",
            status="completed",
            overall_result="false",
            language="en",
            privacy_mode_snapshot=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(analysis)
        await session.flush()

        claim = Claim(
            analysis_id=analysis.id,
            claim_text="x",
            claim_order=0,
            result="false",
            investigation_complete=True,
            reasoning_text="x",
        )
        session.add(claim)
        await session.flush()

        evidence = Evidence(claim_id=claim.id, stance="neutral", retrieved_at=datetime.now(timezone.utc))
        session.add(evidence)

        analysis_session = AnalysisSession(user_id=user_id, name="Session")
        session.add(analysis_session)

        scheduled_check = ScheduledCheck(
            user_id=user_id,
            source_analysis_id=analysis.id,
            claim_text_snapshot="x",
            language="en",
            scheduled_for=datetime.now(timezone.utc),
        )
        session.add(scheduled_check)

        notification = Notification(user_id=user_id, notification_type="security_event", title="t", body="b")
        session.add(notification)

        await session.commit()
        return {
            "analysis_id": analysis.id,
            "claim_id": claim.id,
            "evidence_id": evidence.id,
            "session_id": analysis_session.id,
            "scheduled_check_id": scheduled_check.id,
            "notification_id": notification.id,
        }


@pytest.mark.asyncio
async def test_delete_account_requires_auth(auth_env):
    resp = await auth_env.client.request(
        "DELETE", "/api/v1/account", json={"confirmation_phrase": "DELETE"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_account_requires_exact_confirmation_phrase(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "delete"},  # wrong case
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_account_removes_all_owned_data(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    ids = await _seed_full_account_data(auth_env.sessionmaker, user_id)

    resp = await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "DELETE"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["analyses_deleted"] == 1
    assert body["scheduled_checks_deleted"] == 1
    assert body["sessions_deleted"] == 1
    assert body["notifications_deleted"] == 1

    async with auth_env.sessionmaker() as session:
        for model, row_id in [
            (Analysis, ids["analysis_id"]),
            (Claim, ids["claim_id"]),
            (Evidence, ids["evidence_id"]),
            (AnalysisSession, ids["session_id"]),
            (ScheduledCheck, ids["scheduled_check_id"]),
            (Notification, ids["notification_id"]),
        ]:
            result = await session.execute(select(model).where(model.id == row_id))
            assert result.scalar_one_or_none() is None, f"{model.__name__} row survived deletion"


@pytest.mark.asyncio
async def test_delete_account_deletes_dashboard_account_and_otp_rows(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)

    await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "DELETE"},
        headers={"Authorization": f"Bearer {token}"},
    )

    async with auth_env.sessionmaker() as session:
        dash_result = await session.execute(select(DashboardAccount).where(DashboardAccount.user_id == user_id))
        assert dash_result.scalar_one_or_none() is None

        otp_result = await session.execute(select(OtpVerification).where(OtpVerification.user_id == user_id))
        assert otp_result.scalars().all() == []


@pytest.mark.asyncio
async def test_delete_account_anonymizes_phone_and_soft_deletes_user(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)

    async with auth_env.sessionmaker() as session:
        before_result = await session.execute(select(User).where(User.id == user_id))
        original_hash = before_result.scalar_one().phone_number_hash

    await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "DELETE"},
        headers={"Authorization": f"Bearer {token}"},
    )

    user = await _get_user_by_id(auth_env.sessionmaker, user_id)
    assert user.is_deleted is True
    assert user.deleted_at is not None
    assert user.phone_number_hash != original_hash  # irreversibly re-hashed


@pytest.mark.asyncio
async def test_token_is_immediately_invalidated_after_deletion(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}

    await auth_env.client.request("DELETE", "/api/v1/account", json={"confirmation_phrase": "DELETE"}, headers=headers)

    # The SAME still-unexpired, never-logged-out token must now be rejected.
    resp = await auth_env.client.get("/api/v1/me", headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_double_deletion_attempt_fails_cleanly(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}
    await auth_env.client.request("DELETE", "/api/v1/account", json={"confirmation_phrase": "DELETE"}, headers=headers)

    resp = await auth_env.client.request("DELETE", "/api/v1/account", json={"confirmation_phrase": "DELETE"}, headers=headers)
    assert resp.status_code == 401  # token already invalid, not a 500


@pytest.mark.asyncio
async def test_delete_account_does_not_touch_other_users_data(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_full_account_data(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    user_b_id = await _get_user_id(auth_env.sessionmaker)
    ids_b = await _seed_full_account_data(auth_env.sessionmaker, user_b_id)

    await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "DELETE"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    # User B's data and account are completely untouched.
    user_b = await _get_user_by_id(auth_env.sessionmaker, user_b_id)
    assert user_b.is_deleted is False

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(Analysis).where(Analysis.id == ids_b["analysis_id"]))
        assert result.scalar_one_or_none() is not None

    resp_b = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_b.status_code == 200


@pytest.mark.asyncio
async def test_delete_account_writes_surviving_audit_log_entry(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)

    await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "DELETE"},
        headers={"Authorization": f"Bearer {token}"},
    )

    async with auth_env.sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.action == "account_deleted", AuditLog.entity_id == user_id)
        )
        entries = result.scalars().all()
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_delete_account_never_leaks_phone_number_in_response(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.request(
        "DELETE",
        "/api/v1/account",
        json={"confirmation_phrase": "DELETE"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert PHONE_A not in resp.text
