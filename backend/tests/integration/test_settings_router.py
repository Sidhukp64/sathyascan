"""
Integration tests for the Phase 6 Settings API (app/api/v1/routers/settings.py)
— language + privacy mode, using tests/conftest.py's shared `auth_env`
fixture and `login_and_get_token` helper.
"""

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812345201"
PHONE_B = "919812345202"


@pytest.mark.asyncio
async def test_get_language_settings_requires_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/settings/language")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_language_settings_defaults(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/settings/language", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["preferred_language"] == "en"
    assert body["translated_replies_available"] is True


@pytest.mark.asyncio
async def test_put_language_settings_valid_supported_code(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.put(
        "/api/v1/settings/language",
        json={"preferred_language": "ml"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["preferred_language"] == "ml"
    assert body["translated_replies_available"] is True  # ml is actually rendered


@pytest.mark.asyncio
async def test_put_language_settings_hindi_and_tamil_now_fully_supported(auth_env):
    """hi/ta are documented, valid, PERSISTABLE codes, and — since the
    Hindi/Tamil localization fix (app/i18n/templates.py's
    SUPPORTED_LANGUAGES now covers all 4 documented codes) — DO get real
    translated replies, same honest signal previously only true for ml."""
    token = await login_and_get_token(auth_env, PHONE_A)
    for language in ("hi", "ta"):
        resp = await auth_env.client.put(
            "/api/v1/settings/language",
            json={"preferred_language": language},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["preferred_language"] == language
        assert body["translated_replies_available"] is True  # the honest signal, now true for hi/ta too


@pytest.mark.asyncio
async def test_put_language_settings_rejects_undocumented_code(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.put(
        "/api/v1/settings/language",
        json={"preferred_language": "fr"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_language_settings_persist_across_requests(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    await auth_env.client.put(
        "/api/v1/settings/language",
        json={"preferred_language": "ta"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = await auth_env.client.get(
        "/api/v1/settings/language", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.json()["preferred_language"] == "ta"


@pytest.mark.asyncio
async def test_language_settings_isolated_per_user(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    token_b = await login_and_get_token(auth_env, PHONE_B)

    await auth_env.client.put(
        "/api/v1/settings/language",
        json={"preferred_language": "ml"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    resp_b = await auth_env.client.get(
        "/api/v1/settings/language", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp_b.json()["preferred_language"] == "en"  # untouched default


@pytest.mark.asyncio
async def test_get_privacy_settings_default_is_true(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    resp = await auth_env.client.get(
        "/api/v1/settings/privacy", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["privacy_mode"] is True


@pytest.mark.asyncio
async def test_put_privacy_settings_toggles_and_persists(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    put_resp = await auth_env.client.put(
        "/api/v1/settings/privacy",
        json={"privacy_mode": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["privacy_mode"] is False

    get_resp = await auth_env.client.get(
        "/api/v1/settings/privacy", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.json()["privacy_mode"] is False


@pytest.mark.asyncio
async def test_privacy_settings_isolated_per_user(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    token_b = await login_and_get_token(auth_env, PHONE_B)

    await auth_env.client.put(
        "/api/v1/settings/privacy",
        json={"privacy_mode": False},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    resp_b = await auth_env.client.get(
        "/api/v1/settings/privacy", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp_b.json()["privacy_mode"] is True  # untouched default


@pytest.mark.asyncio
async def test_settings_changes_write_audit_log_without_leaking_phone(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    await auth_env.client.put(
        "/api/v1/settings/language",
        json={"preferred_language": "ml"},
        headers={"Authorization": f"Bearer {token}"},
    )
    await auth_env.client.put(
        "/api/v1/settings/privacy",
        json={"privacy_mode": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    async with auth_env.sessionmaker() as session:
        result = await session.execute(select(AuditLog))
        entries = result.scalars().all()

    actions = {e.action for e in entries}
    assert "language_changed" in actions
    assert "privacy_mode_changed" in actions
    for entry in entries:
        assert PHONE_A not in str(entry.audit_metadata or {})


@pytest.mark.asyncio
async def test_put_privacy_settings_requires_auth(auth_env):
    resp = await auth_env.client.put("/api/v1/settings/privacy", json={"privacy_mode": False})
    assert resp.status_code == 401
