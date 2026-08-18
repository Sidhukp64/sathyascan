"""
Phone number encryption (decisions.md §7): AES-256-GCM, envelope-style
(nonce stored alongside ciphertext, both in the single bytea column).

Scope note: decisions.md specifies "KMS-managed" envelope encryption. This
sandbox has no cloud KMS available, so PHONE_ENCRYPTION_KEY is a locally
configured 32-byte key read from the environment — the same algorithm
(AES-256-GCM) decisions.md specifies, but the key comes from a config
variable rather than a cloud KMS service. This is an interim, documented
substitution: swap `encrypt_phone_number`/`decrypt_phone_number`'s key
source for a real KMS client when one is integrated, without touching any
caller (the User model just stores whatever bytes come out of this module).
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LENGTH_BYTES = 12


class PhoneEncryptionError(Exception):
    pass


def _load_key(key_b64: str) -> bytes:
    if not key_b64:
        raise PhoneEncryptionError(
            "PHONE_ENCRYPTION_KEY is not configured — refusing to encrypt/decrypt "
            "phone numbers with no key."
        )
    try:
        key = base64.b64decode(key_b64)
    except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
        raise PhoneEncryptionError("PHONE_ENCRYPTION_KEY is not valid base64.") from exc
    if len(key) != 32:
        raise PhoneEncryptionError("PHONE_ENCRYPTION_KEY must decode to exactly 32 bytes (AES-256).")
    return key


def generate_encryption_key() -> str:
    """Utility for operators setting up a new environment — not called at runtime."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def encrypt_phone_number(phone_number: str, key_b64: str) -> bytes:
    key = _load_key(key_b64)
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LENGTH_BYTES)
    ciphertext = aesgcm.encrypt(nonce, phone_number.encode("utf-8"), associated_data=None)
    return nonce + ciphertext


def decrypt_phone_number(encrypted: bytes, key_b64: str) -> str:
    key = _load_key(key_b64)
    aesgcm = AESGCM(key)
    nonce, ciphertext = encrypted[:_NONCE_LENGTH_BYTES], encrypted[_NONCE_LENGTH_BYTES:]
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    except Exception as exc:  # noqa: BLE001
        raise PhoneEncryptionError("Failed to decrypt phone number — wrong key or corrupted data.") from exc
    return plaintext.decode("utf-8")
