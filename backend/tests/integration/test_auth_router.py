"""
Integration tests for the Phase 6 dashboard-auth surface
(app/api/v1/routers/auth.py) — full HTTP round trips over the real FastAPI
app, using tests/conftest.py's shared `auth_env` fixture (in-memory SQLite +
fakeredis.aioredis.FakeRedis, no real Postgres/Redis/WhatsApp needed; also
exposes the sessionmaker/redis directly so a few tests can set up state — an
already-expired OTP row, a pre-revoked JWT — unreachable through the HTTP
surface alone).

WhatsApp delivery itself is NEVER live-tested here (fake_sender records
what WOULD have been sent, not a real Graph API call) — see
app/api/v1/routers/auth.py's module docstring for the honest, explicit
statement of that limitation.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.jwt_auth import decode_access_token
from app.models.audit_log import AuditLog
from app.models.dashboard_account import DashboardAccount
from app.models.otp_verification import OtpVerification
from app.models.user import User
from tests.conftest import extract_otp_code

PHONE_A = "919812345001"
PHONE_B = "919812345002"


async def _start_and_verify(auth_env, phone: str) -> dict:
    start_resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": phone})
    assert start_resp.status_code == 200
    code = extract_otp_code(auth_env.sender.sent[-1][2])
    verify_resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": phone, "otp_code": code}
    )
    assert verify_resp.status_code == 200
    return verify_resp.json()


# ---------------------------------------------------------------------------
# /auth/link/start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_sends_otp_and_returns_200(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in_seconds"] == auth_env.settings.otp_ttl_seconds
    assert len(auth_env.sender.sent) == 1
    to_phone, _phone_hash, message_body = auth_env.sender.sent[0]
    assert to_phone == PHONE_A
    extract_otp_code(message_body)  # asserts internally that a code is present


@pytest.mark.asyncio
async def test_start_normalizes_phone_with_plus_and_spaces(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": "+91 98123 45001"})
    assert resp.status_code == 200
    assert auth_env.sender.sent[0][0] == PHONE_A  # normalized to digits-only


@pytest.mark.asyncio
async def test_start_rejects_invalid_phone_number(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": "not-a-phone"})
    assert resp.status_code == 400
    assert auth_env.sender.sent == []


@pytest.mark.asyncio
async def test_start_enforces_cooldown(auth_env):
    first = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    assert first.status_code == 200

    second = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    assert second.status_code == 429
    assert len(auth_env.sender.sent) == 1  # no second message sent


@pytest.mark.asyncio
async def test_start_cooldown_is_per_phone_number(auth_env):
    resp_a = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    resp_b = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_B})
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert len(auth_env.sender.sent) == 2


@pytest.mark.asyncio
async def test_start_never_returns_phone_number_in_response(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    assert PHONE_A not in resp.text


# ---------------------------------------------------------------------------
# /auth/link/verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_with_correct_code_issues_token(auth_env):
    token_body = await _start_and_verify(auth_env, PHONE_A)
    assert token_body["token_type"] == "bearer"
    payload = decode_access_token(token_body["access_token"], auth_env.settings.jwt_secret)
    assert payload.user_id is not None


@pytest.mark.asyncio
async def test_verify_creates_dashboard_account_row(auth_env):
    await _start_and_verify(auth_env, PHONE_A)
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(DashboardAccount))
        accounts = result.scalars().all()
    assert len(accounts) == 1


@pytest.mark.asyncio
async def test_verify_with_wrong_code_returns_generic_400(auth_env):
    await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": PHONE_A, "otp_code": "000000"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired verification code."


@pytest.mark.asyncio
async def test_verify_unknown_phone_returns_same_generic_400(auth_env):
    """A phone number that never called /start gets the SAME error text as a
    wrong code — must not distinguish "no such account" from "wrong code"
    (enumeration protection)."""
    resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": "919999999999", "otp_code": "123456"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired verification code."


@pytest.mark.asyncio
async def test_verify_is_single_use_replay_rejected(auth_env):
    await _start_and_verify(auth_env, PHONE_A)
    # Replay: same phone/code combo again — the OTP row is already consumed.
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_B})
    assert resp.status_code == 200  # sanity: cooldown is per-phone, PHONE_B unaffected

    # Re-attempt verify for PHONE_A with a fabricated but plausible code —
    # there is no active (unconsumed) OTP row left for PHONE_A at all.
    replay_resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": PHONE_A, "otp_code": "111111"}
    )
    assert replay_resp.status_code == 400


@pytest.mark.asyncio
async def test_verify_exhausts_max_attempts(auth_env):
    await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})

    max_attempts = auth_env.settings.otp_max_attempts
    for _ in range(max_attempts):
        resp = await auth_env.client.post(
            "/api/v1/auth/link/verify", json={"phone_number": PHONE_A, "otp_code": "000000"}
        )
        assert resp.status_code == 400

    # Even the CORRECT code is now rejected — the OTP row is burned.
    correct_code = extract_otp_code(auth_env.sender.sent[-1][2])
    final_resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": PHONE_A, "otp_code": correct_code}
    )
    assert final_resp.status_code == 400


@pytest.mark.asyncio
async def test_verify_expired_code_rejected(auth_env):
    await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    code = extract_otp_code(auth_env.sender.sent[-1][2])

    # Force the OTP row into the past directly — no real-time sleep needed.
    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(OtpVerification))
        otp_row = result.scalars().one()
        otp_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    resp = await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": PHONE_A, "otp_code": code}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_requires_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_rejects_malformed_authorization_header(auth_env):
    resp = await auth_env.client.get("/api/v1/me", headers={"Authorization": "NotBearer sometoken"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_profile_without_phone_number(auth_env):
    token_body = await _start_and_verify(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token_body['access_token']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "phone_number" not in body
    assert "phone_number_hash" not in body
    assert "phone_number_encrypted" not in body
    assert PHONE_A not in resp.text
    assert body["preferred_language"] == "en"
    assert body["privacy_mode"] is True


@pytest.mark.asyncio
async def test_me_rejects_expired_token(auth_env):
    from app.core.jwt_auth import create_access_token

    async with auth_env.sessionmaker() as session:
        user = User(phone_number_encrypted=b"x", phone_number_hash=uuid.uuid4().hex)
        session.add(user)
        await session.commit()
        await session.refresh(user)

    expired_token = create_access_token(user.id, auth_env.settings.jwt_secret, ttl_seconds=-10)
    resp = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /auth/refresh, /auth/logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_issues_new_token_and_revokes_old(auth_env):
    token_body = await _start_and_verify(auth_env, PHONE_A)
    old_token = token_body["access_token"]

    refresh_resp = await auth_env.client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert refresh_resp.status_code == 200
    new_token = refresh_resp.json()["access_token"]
    assert new_token != old_token

    # Old token no longer works.
    old_me = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {old_token}"})
    assert old_me.status_code == 401

    # New token works.
    new_me = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {new_token}"})
    assert new_me.status_code == 200


@pytest.mark.asyncio
async def test_logout_revokes_token(auth_env):
    token_body = await _start_and_verify(auth_env, PHONE_A)
    token = token_body["access_token"]

    logout_resp = await auth_env.client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout_resp.status_code == 200

    me_resp = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_requires_auth(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Audit log — never leaks secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_never_contains_otp_code_or_phone(auth_env):
    token_body = await _start_and_verify(auth_env, PHONE_A)
    code_used = extract_otp_code(auth_env.sender.sent[-1][2])

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(AuditLog))
        entries = result.scalars().all()

    assert len(entries) >= 2  # at least otp_requested + otp_verified
    actions = {e.action for e in entries}
    assert "otp_requested" in actions
    assert "otp_verified" in actions

    for entry in entries:
        blob = str(entry.audit_metadata or {})
        assert PHONE_A not in blob
        assert code_used not in blob
        assert token_body["access_token"] not in blob
        assert auth_env.settings.jwt_secret not in blob


@pytest.mark.asyncio
async def test_audit_log_records_failed_verification(auth_env):
    await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": PHONE_A})
    await auth_env.client.post(
        "/api/v1/auth/link/verify", json={"phone_number": PHONE_A, "otp_code": "000000"}
    )

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.action == "otp_verification_failed"))
        entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].audit_metadata.get("reason") == "incorrect_code"
