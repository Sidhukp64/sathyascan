"""
Integration test for Phase 9 suspension enforcement on the WhatsApp side
(app/webhook/whatsapp/router.py) — a suspended user gets a distinct decline
reply instead of the normal ack+result flow, and the real fact-checking
pipeline never runs for them.

Uses a local fixture (not the shared `client` fixture from conftest.py)
because this test needs direct DB access to seed `is_suspended=True` after
the user's first message auto-creates their row — `client` intentionally
doesn't expose its sessionmaker (many other tests depend on it staying a
bare `httpx.AsyncClient`), so duplicating just enough of its wiring here
(same fakes, same pattern) is simpler and safer than changing that shared
fixture's shape.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.session import build_sessionmaker
from app.main import create_app
from app.models import Base
from app.models.user import User
from app.webhook.whatsapp.router import get_db_sessionmaker, get_llm_client, get_search_provider, get_sender
from tests.conftest import TEST_APP_SECRET, TEST_VERIFY_TOKEN

import pytest


@pytest_asyncio.fixture
async def suspension_env(test_settings, fake_sender, fake_llm, fake_search):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_sender] = lambda: fake_sender
    app.dependency_overrides[get_llm_client] = lambda: fake_llm
    app.dependency_overrides[get_search_provider] = lambda: fake_search

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = build_sessionmaker(engine)
    app.dependency_overrides[get_db_sessionmaker] = lambda: sessionmaker

    async with app.router.lifespan_context(app):
        app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield SimpleNamespace(client=ac, sessionmaker=sessionmaker, sender=fake_sender)

    await engine.dispose()


def _sign(body: bytes) -> str:
    digest = hmac.new(TEST_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _text_message_payload(wamid: str, from_phone: str, text: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": from_phone, "id": wamid, "type": "text", "text": {"body": text}}
                            ]
                        }
                    }
                ]
            }
        ]
    }


async def _post_webhook(client, payload: dict):
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"}
    )


@pytest.mark.asyncio
async def test_suspended_user_gets_decline_reply_not_the_real_pipeline(suspension_env):
    from_phone = "15559990001"

    # First message: creates the user row, runs normally (ack + result).
    resp1 = await _post_webhook(suspension_env.client, _text_message_payload("wamid.S1", from_phone, "hello"))
    assert resp1.status_code == 200
    sent_before = len(suspension_env.sender.sent)
    assert sent_before >= 1  # at least the ack

    async with suspension_env.sessionmaker() as session:
        result = await session.execute(select(User))
        user = result.scalars().one()
        user.is_suspended = True
        await session.commit()

    resp2 = await _post_webhook(suspension_env.client, _text_message_payload("wamid.S2", from_phone, "check this claim"))
    assert resp2.status_code == 200

    # Exactly ONE new message was sent — the suspension decline — never an
    # "Analyzing..." ack (that would mean the real pipeline started).
    new_messages = suspension_env.sender.sent[sent_before:]
    assert len(new_messages) == 1
    reply_text = new_messages[0][2]
    assert "suspended" in reply_text.lower()
    assert "Analyzing" not in reply_text and "🔍" not in reply_text
