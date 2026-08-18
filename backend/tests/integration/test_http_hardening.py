"""
Phase 7 — app/core/http_hardening.py's SecurityHeadersMiddleware and
RequestSizeLimitMiddleware, tested against the real app instance (not the
class in isolation) so the actual middleware ordering is what's proven.
"""

import pytest


@pytest.mark.asyncio
async def test_security_headers_present_on_normal_response(auth_env):
    resp = await auth_env.client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_api_v1_responses_are_never_cached(auth_env):
    resp = await auth_env.client.get("/api/v1/me")
    # Even a 401 (no auth provided) still gets the no-store header — the
    # header is applied by path prefix, not conditioned on response status.
    assert resp.status_code == 401
    assert resp.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_health_endpoint_is_not_forced_no_store(auth_env):
    resp = await auth_env.client.get("/health")
    # /health is outside /api/v1/* — no forced no-store from THIS middleware
    # (it may still be uncached for other reasons, just not this rule).
    assert resp.headers.get("cache-control") != "no-store"


@pytest.mark.asyncio
async def test_oversized_api_body_rejected_with_413(auth_env):
    oversized_phone = "9" * 100_000
    resp = await auth_env.client.post(
        "/api/v1/auth/link/start",
        content=f'{{"phone_number": "{oversized_phone}"}}'.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_413_response_still_carries_security_headers(auth_env):
    oversized_phone = "9" * 100_000
    resp = await auth_env.client.post(
        "/api/v1/auth/link/start",
        content=f'{{"phone_number": "{oversized_phone}"}}'.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_normal_sized_request_is_not_rejected(auth_env):
    resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": "919812345601"})
    assert resp.status_code != 413


@pytest.mark.asyncio
async def test_webhook_route_unaffected_by_api_v1_size_limit(auth_env):
    """The size-limit middleware only scopes to /api/v1/* — the WhatsApp
    webhook's own signature-verification path must still run normally, not
    get intercepted by dashboard-specific hardening."""
    resp = await auth_env.client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    # Wrong token -> 403 from the webhook's own logic, NOT a 413 from the
    # size-limit middleware misfiring on an unrelated route.
    assert resp.status_code == 403
