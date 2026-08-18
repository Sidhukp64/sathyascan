import hashlib
import hmac

import pytest

from app.core.security import hash_phone_number, verify_webhook_signature


class TestHashPhoneNumber:
    def test_deterministic_for_same_input(self):
        h1 = hash_phone_number("15551234567", "pepper")
        h2 = hash_phone_number("15551234567", "pepper")
        assert h1 == h2

    def test_different_numbers_produce_different_hashes(self):
        h1 = hash_phone_number("15551234567", "pepper")
        h2 = hash_phone_number("15559999999", "pepper")
        assert h1 != h2

    def test_different_pepper_produces_different_hash(self):
        h1 = hash_phone_number("15551234567", "pepper-a")
        h2 = hash_phone_number("15551234567", "pepper-b")
        assert h1 != h2

    def test_output_is_not_the_raw_number(self):
        h = hash_phone_number("15551234567", "pepper")
        assert "15551234567" not in h

    def test_empty_pepper_raises(self):
        with pytest.raises(ValueError):
            hash_phone_number("15551234567", "")


class TestVerifyWebhookSignature:
    def _sign(self, secret: str, body: bytes) -> str:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_valid_signature_passes(self):
        body = b'{"hello": "world"}'
        secret = "shh"
        header = self._sign(secret, body)
        assert verify_webhook_signature(secret, body, header) is True

    def test_tampered_body_fails(self):
        secret = "shh"
        header = self._sign(secret, b'{"hello": "world"}')
        assert verify_webhook_signature(secret, b'{"hello": "WORLD"}', header) is False

    def test_wrong_secret_fails(self):
        body = b'{"hello": "world"}'
        header = self._sign("shh", body)
        assert verify_webhook_signature("different-secret", body, header) is False

    def test_missing_header_fails(self):
        assert verify_webhook_signature("shh", b"body", None) is False

    def test_malformed_header_fails(self):
        assert verify_webhook_signature("shh", b"body", "not-sha256-prefixed") is False

    def test_empty_app_secret_fails_closed(self):
        body = b'{"hello": "world"}'
        header = self._sign("shh", body)
        assert verify_webhook_signature("", body, header) is False
