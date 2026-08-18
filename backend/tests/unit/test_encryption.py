import pytest

from app.core.encryption import PhoneEncryptionError, decrypt_phone_number, encrypt_phone_number, generate_encryption_key

KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 bytes, base64


def test_roundtrip():
    encrypted = encrypt_phone_number("15551234567", KEY)
    assert decrypt_phone_number(encrypted, KEY) == "15551234567"


def test_ciphertext_does_not_contain_plaintext():
    encrypted = encrypt_phone_number("15551234567", KEY)
    assert b"15551234567" not in encrypted


def test_two_encryptions_of_same_number_differ_due_to_random_nonce():
    e1 = encrypt_phone_number("15551234567", KEY)
    e2 = encrypt_phone_number("15551234567", KEY)
    assert e1 != e2  # random nonce each time — not deterministic like the hash


def test_wrong_key_fails_to_decrypt():
    other_key = generate_encryption_key()
    encrypted = encrypt_phone_number("15551234567", KEY)
    with pytest.raises(PhoneEncryptionError):
        decrypt_phone_number(encrypted, other_key)


def test_empty_key_raises():
    with pytest.raises(PhoneEncryptionError):
        encrypt_phone_number("15551234567", "")


def test_generate_encryption_key_produces_valid_32_byte_key():
    key = generate_encryption_key()
    # Should itself round-trip successfully.
    encrypted = encrypt_phone_number("15551234567", key)
    assert decrypt_phone_number(encrypted, key) == "15551234567"
