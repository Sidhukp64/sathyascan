"""
Admin password hashing (Phase 9, roadmap §9.1). A real, slow, salted KDF
(bcrypt) — deliberately NOT `app/core/security.py`'s keyed-HMAC pattern.

Phone numbers and OTP codes (hashed elsewhere in this codebase) are not
user-chosen secrets: they're looked up, not guessed, so a fast keyed hash
with a server-side pepper is the right, sufficient tool. A password IS a
user-chosen, often low-entropy secret — if an attacker ever obtains the
`admin_users.password_hash` column (a DB dump, a backup leak), a fast hash
would let them brute-force it offline in a reasonable time; bcrypt's
built-in work factor makes that infeasible at scale. Using the wrong tool
here would be a real security regression, not a style choice.

Never logs a raw password or its hash.
"""

import bcrypt

_BCRYPT_ROUNDS = 12  # bcrypt's own recommended-as-of-2024 default work factor


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Refusing to hash an empty password.")
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash (e.g. a corrupted/legacy value) — never raise into
        # an auth check; treat as "does not match", same fail-safe posture
        # as every other credential check in this codebase.
        return False


def normalize_admin_email(email: str) -> str:
    """Admins log in by email; stored/compared lowercased so the lookup and
    the UNIQUE constraint are case-insensitive without depending on a
    citext extension (see app/models/admin_user.py's docstring)."""
    return email.strip().lower()
