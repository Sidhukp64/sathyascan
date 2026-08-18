"""
Phase 8 — app/agent/notifications.py. Proves: (1) in_app notifications are
always "delivered" immediately, (2) whatsapp-channel notifications are
ALWAYS created with delivery_status='blocked_by_policy' and the real
WhatsAppSender is never touched (there is no sender object passed into this
module's functions at all — the abstraction structurally cannot send),
(3) rendering is correct per-language with an honest en fallback.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.notifications import (
    create_notification,
    notify_account_deleted,
    notify_scheduled_check_failed,
    notify_scheduled_check_result,
    render_credibility_changed,
    render_scheduled_check_completed,
    render_scheduled_check_failed,
)
from app.db.base import Base
from app.db.session import build_sessionmaker
from app.models.notification import Notification


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


def test_render_scheduled_check_completed_english():
    title, body = render_scheduled_check_completed("Earth is flat", "false", "en")
    assert title == "Re-check complete"
    assert "Earth is flat" in body
    assert "false" in body


def test_render_uses_real_hindi_and_tamil_translations():
    """Hindi/Tamil localization fix: hi/ta now render their OWN
    NOTIFICATION_STRINGS entries (previously absent, silently falling back
    to English) — proven by asserting the title differs from English's."""
    en_title, _ = render_scheduled_check_completed("x", "false", "en")
    hi_title, _ = render_scheduled_check_completed("x", "false", "hi")
    ta_title, _ = render_scheduled_check_completed("x", "false", "ta")
    assert hi_title != en_title
    assert ta_title != en_title


def test_render_falls_back_to_english_for_a_genuinely_unsupported_language():
    title, _ = render_scheduled_check_completed("x", "false", "fr")  # not a documented language code at all
    assert title == "Re-check complete"  # same as English


def test_render_credibility_changed_shows_both_results():
    _, body = render_credibility_changed("claim text", "unverified", "false", "en")
    assert "unverified" in body
    assert "false" in body


@pytest.mark.asyncio
async def test_in_app_notification_is_immediately_delivered(db_session):
    notification = await create_notification(
        db_session,
        user_id=uuid.uuid4(),
        notification_type="scheduled_check_completed",
        title="t",
        body="b",
        channel="in_app",
    )
    assert notification.delivery_status == "delivered"


@pytest.mark.asyncio
async def test_whatsapp_notification_is_always_blocked_by_policy(db_session):
    notification = await create_notification(
        db_session,
        user_id=uuid.uuid4(),
        notification_type="scheduled_check_completed",
        title="t",
        body="b",
        channel="whatsapp",
    )
    assert notification.delivery_status == "blocked_by_policy"


@pytest.mark.asyncio
async def test_notify_scheduled_check_result_creates_both_channels(db_session):
    user_id = uuid.uuid4()
    check_id = uuid.uuid4()
    notifications = await notify_scheduled_check_result(
        db_session,
        user_id=user_id,
        scheduled_check_id=check_id,
        claim_text="claim",
        language="en",
        credibility_changed=False,
        old_result=None,
        new_result="verified",
    )
    channels = {n.channel for n in notifications}
    assert channels == {"in_app", "whatsapp"}
    statuses = {n.channel: n.delivery_status for n in notifications}
    assert statuses["in_app"] == "delivered"
    assert statuses["whatsapp"] == "blocked_by_policy"
    for n in notifications:
        assert n.related_entity_type == "scheduled_check"
        assert n.related_entity_id == check_id


@pytest.mark.asyncio
async def test_notify_scheduled_check_result_uses_changed_template_when_result_differs(db_session):
    notifications = await notify_scheduled_check_result(
        db_session,
        user_id=uuid.uuid4(),
        scheduled_check_id=uuid.uuid4(),
        claim_text="claim",
        language="en",
        credibility_changed=True,
        old_result="unverified",
        new_result="false",
    )
    assert notifications[0].notification_type == "credibility_changed"
    assert "unverified" in notifications[0].body
    assert "false" in notifications[0].body


@pytest.mark.asyncio
async def test_notify_scheduled_check_failed(db_session):
    notification = await notify_scheduled_check_failed(
        db_session, user_id=uuid.uuid4(), scheduled_check_id=uuid.uuid4(), claim_text="x", language="en"
    )
    assert notification.notification_type == "scheduled_check_failed"
    assert notification.delivery_status == "delivered"


@pytest.mark.asyncio
async def test_notify_account_deleted_is_in_app_only(db_session):
    user_id = uuid.uuid4()
    notification = await notify_account_deleted(db_session, user_id=user_id, language="en")
    assert notification.channel == "in_app"
    assert notification.notification_type == "security_event"

    result = await db_session.execute(select(Notification).where(Notification.user_id == user_id))
    rows = result.scalars().all()
    assert len(rows) == 1  # no separate whatsapp row for account deletion
