"""
Integration tests for the Phase 8 Notifications API
(app/api/v1/routers/notifications.py) — full HTTP round trips using
tests/conftest.py's shared `auth_env` fixture.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.notification import Notification
from app.models.user import User
from tests.conftest import login_and_get_token

PHONE_A = "919812345901"
PHONE_B = "919812345902"


async def _seed_notification(sessionmaker, user_id: uuid.UUID, **overrides) -> uuid.UUID:
    async with sessionmaker() as session:
        notification = Notification(
            user_id=user_id,
            notification_type=overrides.get("notification_type", "scheduled_check_completed"),
            channel=overrides.get("channel", "in_app"),
            title=overrides.get("title", "Re-check complete"),
            body=overrides.get("body", "Your re-check finished."),
            delivery_status=overrides.get("delivery_status", "delivered"),
        )
        session.add(notification)
        await session.commit()
        await session.refresh(notification)
        return notification.id


async def _get_user_id(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()).limit(1))
        return result.scalars().one().id


@pytest.mark.asyncio
async def test_list_notifications_requires_auth(auth_env):
    resp = await auth_env.client.get("/api/v1/notifications")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_notifications_isolated_per_user(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_notification(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)

    resp_a = await auth_env.client.get("/api/v1/notifications", headers={"Authorization": f"Bearer {token_a}"})
    resp_b = await auth_env.client.get("/api/v1/notifications", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_a.json()["total"] == 1
    assert resp_b.json()["total"] == 0


@pytest.mark.asyncio
async def test_unread_count_and_filter(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_notification(auth_env.sessionmaker, user_id)
    await _seed_notification(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await auth_env.client.get("/api/v1/notifications", headers=headers)
    body = resp.json()
    assert body["total"] == 2
    assert body["unread_count"] == 2

    # Mark one read.
    notif_id = body["items"][0]["id"]
    await auth_env.client.put(f"/api/v1/notifications/{notif_id}/read", headers=headers)

    resp2 = await auth_env.client.get("/api/v1/notifications", headers=headers)
    assert resp2.json()["unread_count"] == 1

    resp_unread_only = await auth_env.client.get(
        "/api/v1/notifications", params={"unread_only": True}, headers=headers
    )
    assert resp_unread_only.json()["total"] == 1


@pytest.mark.asyncio
async def test_mark_read_cross_user_returns_404(auth_env):
    token_a = await login_and_get_token(auth_env, PHONE_A)
    user_a_id = await _get_user_id(auth_env.sessionmaker)
    notif_id = await _seed_notification(auth_env.sessionmaker, user_a_id)

    token_b = await login_and_get_token(auth_env, PHONE_B)
    resp = await auth_env.client.put(
        f"/api/v1/notifications/{notif_id}/read", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mark_all_read(auth_env):
    token = await login_and_get_token(auth_env, PHONE_A)
    user_id = await _get_user_id(auth_env.sessionmaker)
    await _seed_notification(auth_env.sessionmaker, user_id)
    await _seed_notification(auth_env.sessionmaker, user_id)
    await _seed_notification(auth_env.sessionmaker, user_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await auth_env.client.put("/api/v1/notifications/read-all", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["unread_count"] == 0

    follow_up = await auth_env.client.get("/api/v1/notifications", headers=headers)
    assert follow_up.json()["unread_count"] == 0
