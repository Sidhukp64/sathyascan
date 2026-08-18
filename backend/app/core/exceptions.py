"""
Centralized error handling (PRD §38 / decisions.md §15): the user (and any
webhook caller) never sees a raw exception or stack trace. Internal detail is
logged; the response is always a generic, friendly message.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import log_event

logger = logging.getLogger(__name__)


class InvalidWebhookSignatureError(Exception):
    """Raised when X-Hub-Signature-256 verification fails. Handled as 403."""


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(InvalidWebhookSignatureError)
    async def handle_invalid_signature(request: Request, exc: InvalidWebhookSignatureError):
        log_event(logger, logging.WARNING, "webhook signature rejected", path=str(request.url.path))
        return JSONResponse(status_code=403, content={"detail": "Invalid signature."})

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        log_event(
            logger,
            logging.ERROR,
            "unhandled exception",
            path=str(request.url.path),
            error_type=type(exc).__name__,
        )
        # Webhook callers (Meta) still get a 200-shaped-enough response is NOT
        # correct here — for truly unexpected errors we return 500 so the
        # failure is visible in monitoring; Meta will retry, which is
        # acceptable for a genuine internal fault (as opposed to a signature
        # rejection, which must never be retried the same way).
        return JSONResponse(
            status_code=500,
            content={"detail": "SathyaScan is unable to process this request right now. Please try again later."},
        )
