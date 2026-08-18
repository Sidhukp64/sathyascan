import hashlib
import hmac

import pytest
from starlette.requests import Request

from app.core.config import Settings
from app.core.exceptions import InvalidWebhookSignatureError
from app.webhook.whatsapp.signature import verify_signature_and_get_body

SECRET = "test-app-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _make_request(body: bytes, signature: str | None) -> Request:
    headers = []
    if signature is not None:
        headers.append((b"x-hub-signature-256", signature.encode()))

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhook/whatsapp",
        "headers": headers,
    }
    return Request(scope, receive)


def _settings() -> Settings:
    return Settings(WHATSAPP_APP_SECRET=SECRET)


@pytest.mark.asyncio
async def test_valid_signature_returns_raw_body():
    body = b'{"object": "whatsapp_business_account"}'
    request = _make_request(body, _sign(body))

    result = await verify_signature_and_get_body(request, _settings())

    assert result == body


@pytest.mark.asyncio
async def test_invalid_signature_raises():
    body = b'{"object": "whatsapp_business_account"}'
    request = _make_request(body, "sha256=deadbeef")

    with pytest.raises(InvalidWebhookSignatureError):
        await verify_signature_and_get_body(request, _settings())


@pytest.mark.asyncio
async def test_missing_signature_header_raises():
    body = b'{"object": "whatsapp_business_account"}'
    request = _make_request(body, None)

    with pytest.raises(InvalidWebhookSignatureError):
        await verify_signature_and_get_body(request, _settings())
