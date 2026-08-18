"""
Outbound WhatsApp Cloud API client. Phase 1 only sends plain text replies
(the echo/test response and friendly error/rate-limit messages) — no
templates, no media, no proactive/scheduled sends (decisions.md §4).
"""

import logging
import time

import httpx

from app.core.logging import log_event
from app.core.provider_health import provider_health

logger = logging.getLogger(__name__)


class WhatsAppSendError(Exception):
    """Raised when the Graph API rejects or fails to deliver a message."""


class WhatsAppSender:
    def __init__(self, base_url: str, phone_number_id: str, access_token: str) -> None:
        self._url = f"{base_url}/{phone_number_id}/messages"
        self._access_token = access_token

    async def send_text_message(self, to_phone: str, phone_hash: str, body: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "text",
            "text": {"body": body},
        }
        headers = {"Authorization": f"Bearer {self._access_token}"}

        # Phase 9 — provider health tracking (roadmap §9.5: "WhatsApp/Meta");
        # see app/core/provider_health.py's docstring.
        started_at = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self._url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            provider_health.record_failure("whatsapp", type(exc).__name__)
            # Never log `body` (message content) or the raw `to_phone` — only
            # the hash and outcome, per decisions.md §7/§9/§15.
            log_event(
                logger,
                logging.ERROR,
                "whatsapp send failed",
                phone_hash=phone_hash[:8],
                error_type=type(exc).__name__,
            )
            raise WhatsAppSendError(str(exc)) from exc

        provider_health.record_success("whatsapp", (time.monotonic() - started_at) * 1000)
        log_event(logger, logging.INFO, "whatsapp reply sent", phone_hash=phone_hash[:8])
