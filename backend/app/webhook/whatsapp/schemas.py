"""
Pydantic models for Meta's WhatsApp Cloud API webhook payload and for the
safe, normalized internal message shape produced by parser.py.

The raw-payload models are deliberately permissive (extra="allow", loose
typing on `messages`/`statuses`) because Meta's payload varies by message
type and evolves over time; parser.py is where type-specific, defensive
extraction happens. Schemas here just get us safely from "arbitrary JSON" to
"a shape we can iterate over" without raising on fields we don't know about.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_TEXT_LENGTH = 4096  # WhatsApp's own text message limit; also our safety cap.

SupportedMessageType = Literal["text", "image", "audio", "video", "document", "sticker", "unsupported"]


class WebhookValue(BaseModel):
    model_config = ConfigDict(extra="allow")

    messaging_product: str | None = None
    metadata: dict[str, Any] | None = None
    contacts: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    statuses: list[dict[str, Any]] = Field(default_factory=list)


class WebhookChange(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: WebhookValue
    field: str | None = None


class WebhookEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    changes: list[WebhookChange] = Field(default_factory=list)


class WebhookEnvelope(BaseModel):
    """Top-level shape of a POST /webhook/whatsapp body."""

    model_config = ConfigDict(extra="allow")

    object: str | None = None
    entry: list[WebhookEntry] = Field(default_factory=list)


class MediaMetadata(BaseModel):
    """Metadata only — Phase 1 never downloads or analyzes the media itself."""

    media_id: str
    mime_type: str | None = None
    sha256: str | None = None
    caption: str | None = None
    filename: str | None = None


class NormalizedMessage(BaseModel):
    """Safe internal representation of one inbound user message. This is what
    the router/sender work with — never the raw Meta dict."""

    wamid: str
    from_phone: str  # transient only: used to send the reply and to compute
    # phone_hash; never logged and never persisted (Phase 1 has no database).
    phone_hash: str
    timestamp: str | None = None
    message_type: SupportedMessageType
    raw_type: str  # original Meta `type` field, safe to log (no user content)
    text_body: str | None = None
    media: MediaMetadata | None = None
