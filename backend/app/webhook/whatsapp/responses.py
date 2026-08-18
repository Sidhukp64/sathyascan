"""
Phase 1 response copy. This is a deliberately simple, English-only echo/test
layer — the real evidence-first, multilingual Language Engine described in
agent-architecture.md is Phase 2+. Nothing here performs fact-checking,
analysis, or translation; it only confirms receipt (PRD §38-style: friendly,
never a raw error).
"""

from app.webhook.whatsapp.schemas import NormalizedMessage

_PHASE1_NOTICE = (
    "\n\n(SathyaScan is in early development — this is a connectivity test "
    "response, not a fact-check.)"
)


def build_reply_text(message: NormalizedMessage) -> str:
    if message.message_type == "text":
        return f'✅ SathyaScan received your message: "{message.text_body}"{_PHASE1_NOTICE}'

    if message.message_type in {"image", "audio", "video", "document", "sticker"}:
        return (
            f"📎 SathyaScan received your {message.message_type} — "
            f"content analysis isn't available yet in this test phase.{_PHASE1_NOTICE}"
        )

    return (
        "SathyaScan received your message, but this content type isn't "
        f"supported yet.{_PHASE1_NOTICE}"
    )


def build_rate_limit_reply() -> str:
    return "You're sending messages a little too quickly. Please wait a moment and try again."


def build_unsupported_type_reply() -> str:
    return "SathyaScan received your message, but this content type isn't supported yet."
