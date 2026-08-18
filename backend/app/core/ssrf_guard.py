"""
Reusable SSRF protection utility (decisions.md §15).

NOT YET WIRED to a live fetch path in Phase 2 — the evidence pipeline only
calls the search provider's own fixed API endpoint (integrations/
evidence_search_client.py), never an arbitrary attacker-influenced URL, so
there is no SSRF attack surface to protect yet. This module is built now so
Phase 4's URL Analyzer (which WILL fetch user-submitted/evidence URLs
directly) has it ready — see docs/decisions.md §15 and
docs/risks-and-open-questions.md.

Usage (once wired): resolve the hostname, then call is_safe_ip() on every
resolved address BEFORE making the request. Checking the URL string alone is
not sufficient — DNS rebinding and redirects can point a "safe-looking" URL
at an internal address after the string check passes.
"""

import ipaddress
import socket


def is_safe_ip(ip_str: str) -> bool:
    """Returns False for any private, loopback, link-local, or otherwise
    internal/reserved address — including cloud metadata endpoints
    (169.254.169.254 falls under link-local)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class SSRFBlockedError(Exception):
    pass


def resolve_and_check_hostname(hostname: str) -> list[str]:
    """Resolves a hostname and returns only the safe IPs. Raises
    SSRFBlockedError if EVERY resolved address is unsafe (fail closed) or if
    resolution fails."""
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"Could not resolve host: {hostname}") from exc

    resolved_ips = {info[4][0] for info in addr_infos}
    safe_ips = [ip for ip in resolved_ips if is_safe_ip(ip)]

    if not safe_ips:
        raise SSRFBlockedError(f"All resolved addresses for {hostname} are blocked.")

    return safe_ips
