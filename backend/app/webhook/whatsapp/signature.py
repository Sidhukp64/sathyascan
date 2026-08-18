"""
Webhook signature verification dependency.

Reads the raw request body BEFORE any JSON parsing and verifies it against
X-Hub-Signature-256, per whatsapp-integration.md and decisions.md §15.
Starlette caches `Request.body()` internally, so reading it here does not
prevent the router from later calling `request.json()` on the same request —
no double-read issue.
"""

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.exceptions import InvalidWebhookSignatureError
from app.core.security import verify_webhook_signature


async def verify_signature_and_get_body(request: Request, settings: Settings | None = None) -> bytes:
    """Returns the raw body bytes if the signature is valid; raises
    InvalidWebhookSignatureError otherwise. Call this before touching
    request.json() or any message content."""
    settings = settings or get_settings()
    raw_body = await request.body()
    signature_header = request.headers.get("x-hub-signature-256")

    if not verify_webhook_signature(settings.whatsapp_app_secret, raw_body, signature_header):
        raise InvalidWebhookSignatureError()

    return raw_body
