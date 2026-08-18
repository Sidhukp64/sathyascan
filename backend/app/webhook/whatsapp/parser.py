"""
Turns a raw WebhookEnvelope into a list of safe NormalizedMessage objects.

Responsibilities (Phase 1 scope, docs/decisions.md, whatsapp-integration.md):
- Separate real user messages from delivery "statuses" (read/delivered
  receipts) — statuses are dropped here, never surfaced to the router.
- Extract media METADATA ONLY for image/audio/video/document/sticker — no
  download, no analysis (explicitly out of Phase 1 scope).
- Sanitize text: length-capped, control characters stripped (safe message
  normalization).
- Never raise on a single malformed message — skip it defensively and let
  the rest of the batch (a webhook call can carry multiple messages) proceed.
"""

import logging
import re
from typing import Any

from app.core.logging import log_event
from app.core.security import hash_phone_number
from app.webhook.whatsapp.schemas import (
    MAX_TEXT_LENGTH,
    MediaMetadata,
    NormalizedMessage,
    SupportedMessageType,
    WebhookEnvelope,
)

logger = logging.getLogger(__name__)

_MEDIA_TYPES: set[str] = {"image", "audio", "video", "document", "sticker"}

# Strip C0/C1 control characters except tab/newline/carriage-return.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_PHONE_RE = re.compile(r"^\d{5,20}$")


def _sanitize_text(text: str) -> str:
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    return cleaned[:MAX_TEXT_LENGTH]


def _map_message_type(raw_type: str) -> SupportedMessageType:
    if raw_type == "text":
        return "text"
    if raw_type in _MEDIA_TYPES:
        return raw_type  # type: ignore[return-value]
    return "unsupported"


def _extract_media(raw_type: str, msg: dict[str, Any]) -> MediaMetadata | None:
    media_obj = msg.get(raw_type)
    if not isinstance(media_obj, dict) or "id" not in media_obj:
        return None
    return MediaMetadata(
        media_id=str(media_obj["id"]),
        mime_type=media_obj.get("mime_type"),
        sha256=media_obj.get("sha256"),
        caption=media_obj.get("caption"),
        filename=media_obj.get("filename"),
    )


def parse_normalized_messages(envelope: WebhookEnvelope, phone_hash_pepper: str) -> list[NormalizedMessage]:
    normalized: list[NormalizedMessage] = []

    for entry in envelope.entry:
        for change in entry.changes:
            value = change.value

            # Statuses (delivered/read/failed receipts) are acknowledged by
            # returning 200 at the router level but never processed further.
            if value.statuses:
                log_event(logger, logging.DEBUG, "status events received", count=len(value.statuses))

            for msg in value.messages:
                wamid = msg.get("id")
                from_phone = msg.get("from")

                if not wamid or not from_phone or not _PHONE_RE.match(str(from_phone)):
                    log_event(
                        logger,
                        logging.WARNING,
                        "skipping malformed message: missing/invalid id or from",
                        has_wamid=bool(wamid),
                    )
                    continue

                raw_type = str(msg.get("type", "unknown"))
                message_type = _map_message_type(raw_type)

                text_body: str | None = None
                media: MediaMetadata | None = None

                if message_type == "text":
                    body = (msg.get("text") or {}).get("body")
                    if isinstance(body, str) and body.strip():
                        text_body = _sanitize_text(body)
                    else:
                        message_type = "unsupported"
                elif message_type in _MEDIA_TYPES:
                    media = _extract_media(raw_type, msg)
                    if media is None:
                        message_type = "unsupported"

                try:
                    phone_hash = hash_phone_number(str(from_phone), phone_hash_pepper)
                except ValueError:
                    log_event(logger, logging.ERROR, "phone hashing failed — misconfigured pepper")
                    continue

                normalized.append(
                    NormalizedMessage(
                        wamid=str(wamid),
                        from_phone=str(from_phone),
                        phone_hash=phone_hash,
                        timestamp=msg.get("timestamp"),
                        message_type=message_type,
                        raw_type=raw_type,
                        text_body=text_body,
                        media=media,
                    )
                )

    return normalized
