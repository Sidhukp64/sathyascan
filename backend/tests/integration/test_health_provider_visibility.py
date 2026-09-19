"""
Proves /health's speech_to_text_provider / audio_forensics_provider /
video_forensics_provider fields correctly flip to "real" when credentials
ARE configured — complementing test_webhook.py's default-state ("null")
check. Constructs the app directly (not via the shared `client` fixture,
which always runs with no provider credentials set) so this test controls
the environment precisely, via monkeypatch, without touching any other
test's isolation. Uses the same `app.router.lifespan_context(app)` pattern
tests/conftest.py's `client` fixture uses — no extra dependency needed.
"""

import httpx


async def test_health_reports_real_when_credentials_are_configured(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "x")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "x")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "x")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "x")
    monkeypatch.setenv("PHONE_HASH_PEPPER", "test-pepper")
    monkeypatch.setenv("PHONE_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SPEECH_TO_TEXT_PROVIDER", "sarvam")
    monkeypatch.setenv("SPEECH_TO_TEXT_API_KEY", "fake-key-for-this-test-only")
    monkeypatch.setenv("AUDIO_FORENSICS_PROVIDER", "resemble")
    monkeypatch.setenv("AUDIO_FORENSICS_API_KEY", "fake-key-for-this-test-only")
    # video_forensics deliberately left unconfigured — proves the three
    # fields are independent, not all-or-nothing.

    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.main import create_app

        app = create_app()

        async with app.router.lifespan_context(app):
            import fakeredis.aioredis

            app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/health")

        body = response.json()
        assert body["speech_to_text_provider"] == "real"
        assert body["audio_forensics_provider"] == "real"
        assert body["video_forensics_provider"] == "null"  # unconfigured — must stay null
        # Phase 7 additions — this test sets PHONE_HASH_PEPPER/
        # PHONE_ENCRYPTION_KEY above but deliberately NOT JWT_SECRET,
        # proving all three flags are independently, correctly read.
        assert body["phone_hash_pepper_configured"] is True
        assert body["phone_encryption_key_configured"] is True
        assert body["jwt_configured"] is False
    finally:
        get_settings.cache_clear()
