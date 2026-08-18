"""
Phase 7 — dashboard API rate limiting (decisions.md §12/§15: "Per-user rate
limiting... already designed" + "Rate limiting verified (per decision #12)"
as a release-gate item — never implemented for the dashboard surface in
Phase 6, closed here). Proves both new limiters actually trip, not just that
the code exists.
"""

import pytest

from tests.conftest import login_and_get_token

PHONE_A = "919812345401"
PHONE_B = "919812345402"


@pytest.mark.asyncio
async def test_per_user_rate_limit_trips_after_configured_count(auth_env):
    # Lower the limit for this test so it doesn't need 60 real requests.
    auth_env.settings.dashboard_rate_limit_per_user_per_minute = 3
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}

    statuses = []
    for _ in range(5):
        resp = await auth_env.client.get("/api/v1/me", headers=headers)
        statuses.append(resp.status_code)

    assert statuses.count(200) == 3
    assert statuses.count(429) == 2


@pytest.mark.asyncio
async def test_per_user_rate_limit_is_isolated_per_user(auth_env):
    auth_env.settings.dashboard_rate_limit_per_user_per_minute = 2
    token_a = await login_and_get_token(auth_env, PHONE_A)
    token_b = await login_and_get_token(auth_env, PHONE_B)

    # Exhaust user A's budget.
    for _ in range(2):
        resp = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token_a}"})
        assert resp.status_code == 200
    exhausted = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token_a}"})
    assert exhausted.status_code == 429

    # User B is untouched.
    resp_b = await auth_env.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_b.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_applies_across_different_authenticated_routes(auth_env):
    """The limiter lives in get_current_token_payload, the single dependency
    every protected route shares — proves it's not accidentally scoped to
    just one route."""
    auth_env.settings.dashboard_rate_limit_per_user_per_minute = 2
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}

    r1 = await auth_env.client.get("/api/v1/me", headers=headers)
    r2 = await auth_env.client.get("/api/v1/history", headers=headers)
    r3 = await auth_env.client.get("/api/v1/settings/privacy", headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429  # third call across DIFFERENT routes, same user


@pytest.mark.asyncio
async def test_otp_start_per_ip_rate_limit_trips(auth_env):
    auth_env.settings.otp_start_rate_limit_per_ip_per_minute = 2
    phones = ["919812345501", "919812345502", "919812345503"]

    statuses = []
    for phone in phones:
        resp = await auth_env.client.post("/api/v1/auth/link/start", json={"phone_number": phone})
        statuses.append(resp.status_code)

    # First two (different phone numbers, so the per-phone cooldown never
    # fires) succeed; the third is blocked by the per-IP limiter alone.
    assert statuses[:2] == [200, 200]
    assert statuses[2] == 429


@pytest.mark.asyncio
async def test_rate_limit_response_never_leaks_internal_detail(auth_env):
    auth_env.settings.dashboard_rate_limit_per_user_per_minute = 1
    token = await login_and_get_token(auth_env, PHONE_A)
    headers = {"Authorization": f"Bearer {token}"}

    await auth_env.client.get("/api/v1/me", headers=headers)
    resp = await auth_env.client.get("/api/v1/me", headers=headers)
    assert resp.status_code == 429
    assert token not in resp.text
    assert "ratelimit:" not in resp.text  # internal Redis key shape never echoed
