"""
SSRF guard tests (app/core/ssrf_guard.py) — now load-bearing in Phase 3 via
app/media/downloader.py.
"""

import socket
from unittest.mock import patch

import pytest

from app.core.ssrf_guard import SSRFBlockedError, is_safe_ip, resolve_and_check_hostname


class TestIsSafeIP:
    def test_rejects_loopback(self):
        assert is_safe_ip("127.0.0.1") is False
        assert is_safe_ip("::1") is False

    def test_rejects_private_ranges(self):
        assert is_safe_ip("10.0.0.1") is False
        assert is_safe_ip("172.16.0.1") is False
        assert is_safe_ip("192.168.1.1") is False

    def test_rejects_cloud_metadata_endpoint(self):
        # 169.254.169.254 — AWS/GCP/Azure metadata service, falls under link-local.
        assert is_safe_ip("169.254.169.254") is False

    def test_rejects_unspecified(self):
        assert is_safe_ip("0.0.0.0") is False

    def test_rejects_multicast(self):
        assert is_safe_ip("224.0.0.1") is False

    def test_accepts_public_ip(self):
        assert is_safe_ip("8.8.8.8") is True

    def test_rejects_invalid_string(self):
        assert is_safe_ip("not-an-ip") is False


class TestResolveAndCheckHostname:
    def test_rejects_hostname_resolving_to_localhost(self):
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            with pytest.raises(SSRFBlockedError):
                resolve_and_check_hostname("localhost")

    def test_rejects_hostname_resolving_to_private_ip(self):
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.5", 0))]):
            with pytest.raises(SSRFBlockedError):
                resolve_and_check_hostname("internal.example")

    def test_rejects_hostname_resolving_to_metadata_ip(self):
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]):
            with pytest.raises(SSRFBlockedError):
                resolve_and_check_hostname("attacker.example")

    def test_dns_rebinding_style_check_rejects_when_resolution_is_internal(self):
        """Simulates what a DNS-rebinding attack's resolution would look
        like at the moment this function checks it: a hostname that (at
        resolution time) points at an internal address must be rejected,
        regardless of what a prior/different resolution might have returned."""
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 0))]):
            with pytest.raises(SSRFBlockedError):
                resolve_and_check_hostname("rebinding-target.example")

    def test_accepts_hostname_resolving_to_public_ip(self):
        with patch.object(socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            safe_ips = resolve_and_check_hostname("example.com")
            assert safe_ips == ["93.184.216.34"]

    def test_raises_on_dns_resolution_failure(self):
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("resolution failed")):
            with pytest.raises(SSRFBlockedError):
                resolve_and_check_hostname("nonexistent.invalid")

    def test_mixed_results_only_returns_safe_ips(self):
        with patch.object(
            socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 0)), (2, 1, 6, "", ("127.0.0.1", 0))],
        ):
            safe_ips = resolve_and_check_hostname("mixed.example")
            assert safe_ips == ["8.8.8.8"]
