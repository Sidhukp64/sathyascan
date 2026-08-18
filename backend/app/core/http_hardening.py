"""
Phase 7 — HTTP-level hardening middleware (decisions.md §15's security
release-gate checklist: "the following must be explicitly verified" — this
module covers the two items nothing in Phases 1-6 addressed: response
security headers and a request-body size ceiling).

Two independent, deliberately small pieces of ASGI middleware:

1. `SecurityHeadersMiddleware` — adds standard defensive response headers to
   every response. No CORS middleware is added anywhere in this app —
   that's a deliberate choice, not an oversight: this backend has no
   frontend (confirmed scope decision), so the secure default (browsers
   refuse cross-origin reads without an explicit `Access-Control-Allow-
   Origin`) is exactly the posture wanted; adding a permissive CORS policy
   would be a regression, not a hardening step, until a specific dashboard
   frontend origin actually exists to allowlist.

2. `RequestSizeLimitMiddleware` — rejects a request (413) whose
   `Content-Length` header exceeds a configured ceiling, before the body is
   ever read into a Pydantic model. **Honest limitation**: this checks the
   declared `Content-Length` header only — a request sent with
   `Transfer-Encoding: chunked` and no `Content-Length` is not caught by
   this check (Starlette/uvicorn still buffer the body into memory before
   handing it to the route, so a chunked-encoded oversized body is a real,
   documented gap, not a silently-assumed-fixed one — see
   docs/risks-and-open-questions.md's Phase 7 entry). This only applies to
   the dashboard JSON API (`/api/v1/*`) — the WhatsApp webhook's media
   handling has its own, separate, already-real size enforcement
   (`MAX_UPLOAD_SIZE_BYTES_*`, streamed/capped at the Meta-media-download
   layer, not the inbound webhook body, which is always small JSON).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # The dashboard API returns per-user data (history, settings) — never
        # cacheable by an intermediary or the browser's own disk cache.
        # /docs, /openapi.json, /redoc are static schema, not user data, so
        # left uncached-by-default rather than explicitly forced either way.
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_body_bytes: int) -> None:
        super().__init__(app)
        self._max_body_bytes = max_body_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path.startswith("/api/v1/"):
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > self._max_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large."},
                    )
        return await call_next(request)
