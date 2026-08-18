"""
Detects whether an inbound WhatsApp text message is a bare URL, so
router.py can route it to UrlPipeline instead of TextPipeline's plain
claim-extraction path.

WhatsApp has no distinct "url" message type (whatsapp-integration.md) — a
forwarded link arrives as an ordinary `text` message, same as a typed claim.

Deliberately conservative: only messages whose ENTIRE (stripped) body is a
single valid http(s) URL are routed here. A message that merely MENTIONS a
URL alongside other text (e.g. "Is this true? https://example.com/article")
stays on the existing Phase 2 text-claim path, unchanged — an intentional
Phase 4 scope boundary flagged during the pre-implementation review, not an
oversight: phased-plan.md's done-when criterion is phrased as "forwarding a
URL" (the classic single-link-forward case), and combined URL+commentary
handling is materially more complex (which content to trust, whether to run
both paths) and not required by that criterion.
"""

from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}


def extract_bare_url(text: str | None) -> str | None:
    """Returns the URL if the ENTIRE stripped message is a single valid
    http(s) URL, else None."""
    candidate = (text or "").strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None

    parsed = urlparse(candidate)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        return None

    return candidate
