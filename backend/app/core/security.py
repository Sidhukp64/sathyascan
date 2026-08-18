"""
Security helpers. Phase 1 scope: phone-number hashing and webhook signature
verification only (decisions.md §7, §15). Phase 1 never stores a phone number
anywhere durable — no database exists yet — so there is no
`phone_number_encrypted` column to write here; that arrives with the
database in a later phase. This module still exists now because the hash is
needed immediately for rate-limiting and log-safe identification.

Phase 6 adds OTP code generation/hashing (app/api/v1/auth.py) — reuses the
same keyed-HMAC pattern as hash_phone_number rather than inventing a new
primitive, and deliberately reuses PHONE_HASH_PEPPER rather than adding a
second secret to provision/rotate (both are short, guessable-space secrets
being protected the same way: phone numbers and 6-digit OTP codes are both
small enough spaces that an unsalted/unkeyed hash would be close to
reversible by brute force).
"""

import hashlib
import hmac
import re
import secrets

# Identical bound to app/webhook/whatsapp/parser.py's _PHONE_RE — Meta's
# `from` field is always digits-only (no '+', no separators), so a dashboard-
# submitted phone number MUST be normalized to that exact same shape before
# hashing, or the same real-world number would hash differently depending on
# which entry point it came through and resolve to two different `users`
# rows. That module's regex is private/webhook-scoped; this one is the
# general-purpose version reused by app/api/v1/routers/auth.py.
_NORMALIZED_PHONE_RE = re.compile(r"^\d{5,20}$")


def normalize_phone_number(raw: str) -> str | None:
    """Strips a leading '+' and any spaces/dashes/parens a human might type,
    then validates the result matches the digits-only shape Meta's webhook
    payloads already use. Returns None (not an exception) for invalid input —
    callers are expected to turn that into a 400, not a 500; this is
    user-supplied input, not a config value."""
    stripped = re.sub(r"[\s\-()]", "", raw).removeprefix("+")
    if not _NORMALIZED_PHONE_RE.match(stripped):
        return None
    return stripped


def hash_phone_number(phone_number: str, pepper: str) -> str:
    """Keyed HMAC-SHA256(phone, pepper) — never a bare/unsalted hash
    (decisions.md §7: phone-number space is small/guessable, so an unsalted
    hash is close to reversible). Returns a hex digest safe to log or use as
    a Redis key component.
    """
    if not pepper:
        # Fail loud rather than silently hashing with an empty pepper, which
        # would make the hash trivially guessable — this is a misconfiguration,
        # not a runtime condition to degrade gracefully through.
        raise ValueError(
            "PHONE_HASH_PEPPER is not configured — refusing to hash a phone "
            "number with an empty pepper."
        )
    return hmac.new(
        pepper.encode("utf-8"),
        phone_number.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_webhook_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header: 'sha256=<hex hmac>' computed
    over the raw request body using the app secret. Must be called with the
    exact bytes Meta signed, before any JSON parsing (whatsapp-integration.md).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not app_secret:
        return False

    provided_digest = signature_header.removeprefix("sha256=")
    expected_digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided_digest, expected_digest)


def generate_otp_code(length: int) -> str:
    """Cryptographically secure, zero-padded numeric code (secrets.randbelow,
    not `random` — an OTP is a genuine authentication credential). length is
    caller-supplied (Settings.otp_length) rather than hardcoded so it stays
    configurable, but is bounded here against a pathological config value."""
    if length < 4 or length > 10:
        raise ValueError(f"OTP length must be between 4 and 10 digits, got {length}")
    upper_bound = 10**length
    return str(secrets.randbelow(upper_bound)).zfill(length)


def hash_otp_code(code: str, pepper: str) -> str:
    """Keyed HMAC-SHA256(code, pepper) — same rationale/primitive as
    hash_phone_number: a 6-digit code is a 1-in-a-million guessable space, so
    an unkeyed hash would be close to reversible via a precomputed table.
    Reuses PHONE_HASH_PEPPER (see module docstring) rather than a new secret."""
    if not pepper:
        raise ValueError(
            "PHONE_HASH_PEPPER is not configured — refusing to hash an OTP code with an empty pepper."
        )
    return hmac.new(pepper.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).hexdigest()
