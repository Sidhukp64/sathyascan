"""
Endpoint inventory (api-design.md §G: "Perform an endpoint inventory so no
undocumented endpoint is accidentally exposed"), introduced Phase 7, kept
current through Phase 9. Walks the REAL app's OpenAPI schema (not a
hand-maintained list that could drift) and asserts:

1. The exact set of registered paths matches what's intentionally built —
   catches an accidentally-exposed new route as a test failure, not a
   silent surprise.
2. Every `/api/v1/*` route EXCEPT the deliberately-public ones (pre-auth OTP,
   and Explore — docs/api-design.md: "Explore — PUBLIC, rate-limited, no
   auth required") declares `HTTPBearer` security in its OpenAPI operation —
   catches a future route that forgets `Depends(get_current_user)`/
   `get_current_user_id` before it ever reaches production.
"""

from app.main import create_app

# The complete, intentional route inventory as of Phase 8. A new route
# appearing here that ISN'T also added to this set fails this test — forcing
# a deliberate acknowledgment, not a silent addition.
EXPECTED_PATHS = {
    "/health",
    "/webhook/whatsapp",
    "/api/v1/auth/link/start",
    "/api/v1/auth/link/verify",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/me",
    "/api/v1/history",
    "/api/v1/history/{analysis_id}",
    "/api/v1/history/{analysis_id}/report.pdf",
    "/api/v1/settings/language",
    "/api/v1/settings/privacy",
    "/api/v1/scheduled-checks",
    "/api/v1/scheduled-checks/{check_id}",
    "/api/v1/scheduled-checks/{check_id}/retry",
    "/api/v1/explore/claims",
    "/api/v1/explore/claims/{cluster_id}",
    "/api/v1/explore/categories",
    "/api/v1/notifications",
    "/api/v1/notifications/read-all",
    "/api/v1/notifications/{notification_id}/read",
    "/api/v1/overview",
    "/api/v1/sessions",
    "/api/v1/sessions/{session_id}",
    "/api/v1/sessions/{session_id}/analyses",
    "/api/v1/sessions/{session_id}/analyses/{analysis_id}",
    "/api/v1/account",
    # Phase 9 — Appeals (user-facing)
    "/api/v1/appeals",
    "/api/v1/appeals/{appeal_id}",
    "/api/v1/appeals/{appeal_id}/cancel",
    # Phase 9 — Moderation (user-facing)
    "/api/v1/moderation/reports",
    "/api/v1/moderation/reports/{report_id}",
}

# Endpoints that are deliberately public (no JWT) — everything else under
# /api/v1/* must require HTTPBearer. OTP-start/verify (how a token is
# obtained) and all of /explore/* (locked design decision, api-design.md).
PRE_AUTH_API_PATHS = {
    "/api/v1/auth/link/start",
    "/api/v1/auth/link/verify",
    "/api/v1/explore/claims",
    "/api/v1/explore/claims/{cluster_id}",
    "/api/v1/explore/categories",
}


def _api_v1_operations(schema: dict):
    for path, path_item in schema["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                continue
            yield path, method, operation


def test_no_unexpected_route_is_registered():
    app = create_app()
    schema = app.openapi()
    actual_paths = set(schema["paths"].keys())
    assert actual_paths == EXPECTED_PATHS, (
        f"Route inventory drifted. New: {actual_paths - EXPECTED_PATHS}, "
        f"Missing: {EXPECTED_PATHS - actual_paths}"
    )


def test_every_authenticated_api_route_declares_bearer_security():
    app = create_app()
    schema = app.openapi()

    unprotected = []
    for path, method, operation in _api_v1_operations(schema):
        if path in PRE_AUTH_API_PATHS:
            continue
        security = operation.get("security")
        has_bearer = bool(security) and any("HTTPBearer" in s for s in security)
        if not has_bearer:
            unprotected.append(f"{method.upper()} {path}")

    assert unprotected == [], f"Routes missing HTTPBearer security: {unprotected}"


def test_pre_auth_otp_routes_do_not_declare_bearer_security():
    """The inverse check — /auth/link/start and /verify must NOT require a
    token (they're how a token is obtained in the first place); a stray
    Depends(get_current_user) accidentally added to one of these would lock
    every user out of ever logging in."""
    app = create_app()
    schema = app.openapi()

    for path, method, operation in _api_v1_operations(schema):
        if path not in PRE_AUTH_API_PATHS:
            continue
        security = operation.get("security")
        has_bearer = bool(security) and any("HTTPBearer" in s for s in security)
        assert not has_bearer, f"{method.upper()} {path} unexpectedly requires auth"


def test_webhook_route_has_no_bearer_security():
    """The WhatsApp webhook authenticates via its own HMAC signature check,
    never a JWT — confirms it was never accidentally wrapped in the
    dashboard's auth dependency."""
    app = create_app()
    schema = app.openapi()
    webhook_ops = schema["paths"]["/webhook/whatsapp"]
    for method, operation in webhook_ops.items():
        if method.lower() not in {"get", "post"}:
            continue
        assert not operation.get("security")
