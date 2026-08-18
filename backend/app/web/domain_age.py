"""
Domain-age checking via real WHOIS lookups (stdlib `socket`, port 43) — no
API key needed. Per the user's explicit Phase 4 confirmation, this is
implemented for REAL rather than stubbed like OCR/image-forensics/
threat-intel: WHOIS is an unauthenticated public protocol, genuinely
testable without credentials. A live two-step lookup (IANA TLD referral,
then the registry's own WHOIS server) was confirmed working from this build
environment during implementation — see
tests/integration/test_domain_age.py's live smoke test.

Best-effort and non-blocking to the pipeline by design: WHOIS response
formats are NOT standardized across registries, so a timeout, refused
connection, missing IANA referral, or unparseable response simply omits the
domain-age signal (`DomainAgeResult(status=UNAVAILABLE)`) — it must NEVER
raise into the caller or block the URL Safety assessment. Blocking socket
I/O is run in a thread (`asyncio.to_thread`) so it never stalls the event
loop.
"""

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Protocol

from app.core.logging import log_event

logger = logging.getLogger(__name__)

IANA_WHOIS_SERVER = "whois.iana.org"

# WHOIS output is NOT standardized across registries — these cover the
# common "creation/registration date" field labels seen in practice. This is
# inherently best-effort, never a guaranteed parse.
_CREATION_DATE_PATTERNS = [
    re.compile(r"(?im)^creation date:\s*(.+)$"),
    re.compile(r"(?im)^created(?: on)?:\s*(.+)$"),
    re.compile(r"(?im)^registered(?: on)?:\s*(.+)$"),
    re.compile(r"(?im)^domain registration date:\s*(.+)$"),
]

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%Y.%m.%d",
    "%d.%m.%Y",
)


class DomainAgeStatus(str, Enum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"  # lookup failed/timed out/no referral/unparseable


@dataclass(frozen=True)
class DomainAgeResult:
    status: DomainAgeStatus
    registered_on: date | None = None
    age_days: int | None = None


class DomainAgeChecker(Protocol):
    async def check(self, domain: str) -> DomainAgeResult: ...


def _raw_whois_query(server: str, query: str, timeout_seconds: float) -> str:
    with socket.create_connection((server, 43), timeout=timeout_seconds) as sock:
        sock.sendall((query + "\r\n").encode())
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def _find_registry_referral(iana_response: str) -> str | None:
    for line in iana_response.splitlines():
        if line.lower().startswith("whois:"):
            return line.split(":", 1)[1].strip()
    return None


def _parse_creation_date(whois_text: str) -> date | None:
    for pattern in _CREATION_DATE_PATTERNS:
        match = pattern.search(whois_text)
        if not match:
            continue
        raw_value = match.group(match.lastindex).strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(raw_value, fmt).date()
            except ValueError:
                continue
    return None


def _lookup_sync(domain: str, timeout_seconds: float) -> DomainAgeResult:
    # Registry-level WHOIS only knows registrable domains, not subdomains —
    # best-effort: take the last two DNS labels (e.g. "news.example.co.uk"
    # is intentionally simplified to "example.co.uk" rather than attempting
    # full public-suffix-list parsing, which is out of Phase 4 scope).
    labels = domain.lower().strip(".").split(".")
    if len(labels) < 2:
        return DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)
    tld = labels[-1]
    registrable_domain = ".".join(labels[-2:])

    try:
        iana_response = _raw_whois_query(IANA_WHOIS_SERVER, tld, timeout_seconds)
        registry_server = _find_registry_referral(iana_response)
        if not registry_server:
            return DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)

        registry_response = _raw_whois_query(registry_server, registrable_domain, timeout_seconds)
    except (OSError, socket.timeout):
        return DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)

    creation_date = _parse_creation_date(registry_response)
    if creation_date is None:
        return DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)

    age_days = (datetime.now(timezone.utc).date() - creation_date).days
    return DomainAgeResult(status=DomainAgeStatus.SUCCESS, registered_on=creation_date, age_days=age_days)


class WhoisDomainAgeChecker:
    """Real WHOIS-backed implementation — the only one wired in Phase 4. No
    interface/Null-stub split analogous to OCR/image-analysis/threat-intel
    is needed here, since this feature needs no credentials to work for
    real; `DomainAgeChecker` above still exists as a Protocol purely for
    dependency-injection/testability, matching the codebase's established
    pattern."""

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def check(self, domain: str) -> DomainAgeResult:
        try:
            return await asyncio.to_thread(_lookup_sync, domain, self._timeout_seconds)
        except Exception:  # noqa: BLE001 - must never block/break URL Safety
            log_event(logger, logging.INFO, "WHOIS domain-age lookup failed unexpectedly", domain=domain)
            return DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)
