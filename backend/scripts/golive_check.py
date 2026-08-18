"""
Go-live readiness check for the WhatsApp bot.

Answers one question precisely: **if a user forwards me a text / URL / image
/ voice note right now, what actually happens?**

Reads your real `.env` and reports per-input-type readiness. It NEVER prints
a secret value — only whether each one is set, exactly like `GET /health`.

    python -m scripts.golive_check
"""

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.core.config import get_settings  # noqa: E402

OK, WARN, BAD, DIM, BOLD, END = "\033[0;32m", "\033[0;33m", "\033[0;31m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    OK = WARN = BAD = DIM = BOLD = END = ""

# Windows consoles default to cp1252 and mangle the em-dashes/section signs
# below; ask for real UTF-8 and carry on if the terminal refuses.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - output encoding must never break the check
    pass

TICK, CROSS, DOT = "[OK]", "[--]", "-"


def line(state: str, label: str, detail: str = "") -> None:
    colour = {"ok": OK, "warn": WARN, "bad": BAD}[state]
    mark = {"ok": TICK, "warn": "[!!]", "bad": CROSS}[state]
    print(f"  {colour}{mark}{END} {label}")
    if detail:
        print(f"       {DIM}{detail}{END}")


def main() -> None:
    s = get_settings()

    print(f"\n{BOLD}SathyaScan — WhatsApp go-live check{END}")
    print(f"{DIM}Reads your .env. Never prints secret values.{END}\n")

    # ---------------------------------------------------------------- infra
    print(f"{BOLD}1. Core infrastructure{END}")
    meta_core = all([s.whatsapp_access_token, s.whatsapp_phone_number_id, s.whatsapp_app_secret,
                     s.whatsapp_webhook_verify_token])
    line("ok" if s.whatsapp_access_token else "bad", "WHATSAPP_ACCESS_TOKEN",
         "" if s.whatsapp_access_token else "Meta dashboard -> WhatsApp -> API Setup")
    line("ok" if s.whatsapp_phone_number_id else "bad", "WHATSAPP_PHONE_NUMBER_ID",
         "" if s.whatsapp_phone_number_id else "Meta dashboard -> WhatsApp -> API Setup")
    line("ok" if s.whatsapp_app_secret else "bad", "WHATSAPP_APP_SECRET",
         "" if s.whatsapp_app_secret else "App Settings -> Basic -> App Secret (NOT 'META_APP_SECRET')")
    line("ok" if s.whatsapp_webhook_verify_token else "bad", "WHATSAPP_WEBHOOK_VERIFY_TOKEN",
         "" if s.whatsapp_webhook_verify_token else "You invent this; paste the same value into Meta's webhook config")
    line("ok" if s.whatsapp_business_account_id else "warn", "WHATSAPP_BUSINESS_ACCOUNT_ID",
         "" if s.whatsapp_business_account_id else "Needed for some Graph API calls")

    for label, val, hint in [
        ("JWT_SECRET", s.jwt_secret, 'python -c "import secrets; print(secrets.token_urlsafe(48))"'),
        ("PHONE_HASH_PEPPER", s.phone_hash_pepper, 'python -c "import secrets; print(secrets.token_urlsafe(48))"'),
        ("PHONE_ENCRYPTION_KEY", s.phone_encryption_key,
         'python -c "from app.core.encryption import generate_encryption_key as g; print(g())"'),
    ]:
        line("ok" if val else "bad", label, "" if val else f"generate: {hint}")

    line("ok" if "sqlite" not in s.database_url else "warn", "DATABASE_URL",
         "" if "sqlite" not in s.database_url else "points at SQLite — fine for a demo, use Postgres for production")
    line("ok", "REDIS_URL", f"{s.redis_url} (must be reachable, or OTP login fails)")

    # ------------------------------------------------------------ AI engine
    print(f"\n{BOLD}2. Fact-checking engine{END}")
    ai_ready = bool(s.anthropic_api_key) and bool(s.search_api_key)
    line("ok" if s.anthropic_api_key else "bad", "ANTHROPIC_API_KEY",
         "" if s.anthropic_api_key else "console.anthropic.com — without this NOTHING can be fact-checked")
    line("ok" if s.search_api_key else "bad", "SEARCH_API_KEY",
         "" if s.search_api_key else "tavily.com — without this there is no evidence to cite")

    # ------------------------------------------------- per-forward-type view
    print(f"\n{BOLD}3. What happens when a user forwards...{END}")

    def verdict(ready: bool, label: str, ok_msg: str, bad_msg: str) -> None:
        line("ok" if ready else "warn", label, ok_msg if ready else bad_msg)

    verdict(meta_core and ai_ready, "TEXT",
            "full fact-check with evidence",
            "needs Meta credentials + ANTHROPIC_API_KEY + SEARCH_API_KEY")

    verdict(meta_core and ai_ready, "A LINK (URL)",
            "safety/phishing check + fact-check of the page",
            "safety check works without AI keys; fact-check needs them")

    image_ready = meta_core and ai_ready and s.ocr_provider == "claude_vision" and bool(s.anthropic_api_key)
    verdict(image_ready, "AN IMAGE / SCREENSHOT",
            "text read from the image, then fact-checked",
            'set OCR_PROVIDER=claude_vision (reuses ANTHROPIC_API_KEY) — otherwise replies '
            '"couldn\'t find readable text in this image"')

    audio_ready = meta_core and ai_ready and s.speech_to_text_provider == "sarvam" and bool(s.speech_to_text_api_key)
    verdict(audio_ready, "A VOICE NOTE",
            "transcribed, then fact-checked",
            'set SPEECH_TO_TEXT_PROVIDER=sarvam + SPEECH_TO_TEXT_API_KEY (sarvam.ai) — otherwise replies '
            '"couldn\'t reliably transcribe the audio"')

    # -------------------------------------------------------------- safety
    print(f"\n{BOLD}4. Safety gate{END}")
    if s.safety_gate_provider:
        line("ok", "SAFETY_GATE_PROVIDER configured")
    else:
        line("warn", "SAFETY_GATE_PROVIDER is NOT configured — content-safety checks are a passthrough stub",
             "decisions.md §6: do NOT publish this number publicly. A controlled demo with people you "
             "know is a different risk profile; an open public number is not.")

    # -------------------------------------------------------------- summary
    print(f"\n{BOLD}Summary{END}")
    if not meta_core:
        print(f"  {BAD}Not ready{END} — Meta credentials incomplete. The bot cannot receive or send messages.")
    elif not ai_ready:
        print(f"  {WARN}Partially ready{END} — messages arrive, but nothing can be fact-checked without AI keys.")
    else:
        types = ["text", "URL"] + (["image"] if image_ready else []) + (["audio"] if audio_ready else [])
        print(f"  {OK}Ready for: {', '.join(types)}{END}")
        missing = [t for t, r in [("image", image_ready), ("audio", audio_ready)] if not r]
        if missing:
            print(f"  {DIM}Not yet: {', '.join(missing)} (see section 3 above){END}")
    print(f"\n  {DIM}Full setup guide: docs/whatsapp-golive.md{END}\n")


if __name__ == "__main__":
    main()
