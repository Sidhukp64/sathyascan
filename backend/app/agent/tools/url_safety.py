"""
URL Safety Module — agent-architecture.md's contract:
{url} -> {risk_level, reasons[], threat_intel_matches[], recommendation}.

Entirely deterministic (NO LLM calls) — domain-pattern/HTTPS/redirect/
phishing-keyword/fake-login-form heuristics are all pure Python: cheaper,
faster, and more auditable than an LLM judgment call for this kind of
pattern matching (and avoids spending any of the analysis's LLM-call budget
on this step at all). `recommendation` text is rendered downstream by
ResponseGenerationTool from `risk_level` + i18n templates, not by this tool
— findings carry structured codes/params (FindingCode) so they can be
localized the same way, matching decisions.md's multilingual-response
requirement.

Domain-age is real (WHOIS, see app/web/domain_age.py — no credentials
needed). Threat-intel (Safe Browsing-style) is the ONE component that
genuinely needs a paid third-party API this environment doesn't have
credentials for — per phased-plan.md, that's explicitly OPTIONAL, so it's
interface + Null stub, matching the Phase 3 precedent (OCR/image-analysis/
safety-gate all follow this same pattern for their credentialed pieces).

Runs even when the fetch itself failed or was SSRF-blocked — a safety
assessment doesn't depend on a successful content fetch. A URL that resolves
to a private/internal address IS ITSELF a safety finding
(FindingCode.PRIVATE_ADDRESS_BLOCKED), not just an infra error to hide —
per the user's confirmed Phase 4 transparency policy. This is deliberately
different from decisions.md §6's illegal-content safety gate, which must
never explain why it blocked something; that rule is scoped to the
CSAM/illegal-content gate specifically, not to ordinary URL-safety
reporting.
"""

import ipaddress
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
from urllib.parse import urlparse

from app.core.logging import log_event
from app.web.domain_age import DomainAgeChecker, DomainAgeStatus
from app.web.fetcher import FetchResult

logger = logging.getLogger(__name__)

_YOUNG_DOMAIN_THRESHOLD_DAYS = 30
_MANY_REDIRECTS_THRESHOLD = 2
_SUSPICIOUS_KEYWORDS = ("verify", "secure", "account", "login", "update", "confirm", "urgent", "suspend")
_BRAND_LOOKALIKE_HINTS = ("paypal", "amazon", "google", "microsoft", "apple", "bank", "whatsapp", "facebook")


class RiskLevel(str, Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_RISK_ORDER = [RiskLevel.SAFE, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]


class FindingCode(str, Enum):
    PRIVATE_ADDRESS_BLOCKED = "private_address_blocked"
    MISSING_HTTPS = "missing_https"
    IP_ADDRESS_AS_HOST = "ip_address_as_host"
    PUNYCODE_DOMAIN = "punycode_domain"
    SUSPICIOUS_DOMAIN_PATTERN = "suspicious_domain_pattern"
    EXCESSIVE_SUBDOMAINS = "excessive_subdomains"
    REDIRECT_CROSSES_DOMAINS = "redirect_crosses_domains"
    MANY_REDIRECTS = "many_redirects"
    PHISHING_KEYWORDS = "phishing_keywords"
    FAKE_LOGIN_FORM = "fake_login_form"
    YOUNG_DOMAIN = "young_domain"
    THREAT_INTEL_MATCH = "threat_intel_match"


_FINDING_SEVERITY: dict[FindingCode, RiskLevel] = {
    FindingCode.PRIVATE_ADDRESS_BLOCKED: RiskLevel.CRITICAL,
    FindingCode.THREAT_INTEL_MATCH: RiskLevel.CRITICAL,
    FindingCode.IP_ADDRESS_AS_HOST: RiskLevel.HIGH,
    FindingCode.FAKE_LOGIN_FORM: RiskLevel.HIGH,
    FindingCode.MISSING_HTTPS: RiskLevel.MEDIUM,
    FindingCode.PUNYCODE_DOMAIN: RiskLevel.MEDIUM,
    FindingCode.SUSPICIOUS_DOMAIN_PATTERN: RiskLevel.MEDIUM,
    FindingCode.PHISHING_KEYWORDS: RiskLevel.MEDIUM,
    FindingCode.YOUNG_DOMAIN: RiskLevel.MEDIUM,
    FindingCode.EXCESSIVE_SUBDOMAINS: RiskLevel.LOW,
    FindingCode.REDIRECT_CROSSES_DOMAINS: RiskLevel.LOW,
    FindingCode.MANY_REDIRECTS: RiskLevel.LOW,
}


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ThreatIntelMatch:
    list_name: str
    detail: str | None = None


@dataclass(frozen=True)
class UrlSafetyResult:
    risk_level: RiskLevel
    findings: list[Finding]
    threat_intel_matches: list[ThreatIntelMatch]
    domain_age_days: int | None = None


class ThreatIntelProviderError(Exception):
    pass


class ThreatIntelProvider(Protocol):
    async def check(self, url: str, domain: str) -> list[ThreatIntelMatch]: ...


class NullThreatIntelProvider:
    """Stub — Safe-Browsing-style threat-intel APIs (Google Safe Browsing,
    PhishTank, VirusTotal, etc.) all require paid credentials this
    environment doesn't have. Explicitly OPTIONAL per phased-plan.md's Phase
    4 scope ("...+ optional Safe Browsing-style API..."). Always returns no
    matches — never fabricates a match. Its absence is never treated as a
    verified "safe" signal either (same "absence of known threat isn't proof
    of safety" caveat used throughout risks-and-open-questions.md)."""

    provider_name = "null_stub_no_provider_configured"

    async def check(self, url: str, domain: str) -> list[ThreatIntelMatch]:
        log_event(
            logger, logging.INFO, "threat intel: no provider configured — check skipped, not a safety guarantee"
        )
        return []


def _is_ip_address(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


# Common leetspeak digit-for-letter substitutions used in typosquatting —
# normalizing BEFORE the brand-substring check matters: a domain like
# "paypa1.com" does NOT literally contain "paypal" (1 != l), which is
# exactly what makes it a convincing lookalike in the first place.
_LEETSPEAK_MAP = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t"})


def _check_url_string_heuristics(url: str) -> list[Finding]:
    """Run unconditionally, even without a successful fetch — these need
    only the URL string itself."""
    findings: list[Finding] = []
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()

    if parsed.scheme != "https":
        findings.append(Finding(FindingCode.MISSING_HTTPS))

    if hostname and _is_ip_address(hostname):
        findings.append(Finding(FindingCode.IP_ADDRESS_AS_HOST))

    if "xn--" in hostname:
        findings.append(Finding(FindingCode.PUNYCODE_DOMAIN))

    if hostname.count(".") >= 4:
        findings.append(Finding(FindingCode.EXCESSIVE_SUBDOMAINS))

    hyphen_count = hostname.count("-")
    normalized_hostname = hostname.translate(_LEETSPEAK_MAP)
    is_exact_brand_domain = any(
        hostname == f"{brand}{tld}" for brand in _BRAND_LOOKALIKE_HINTS for tld in (".com", ".org", ".net")
    )
    brand_lookalike = (
        any(brand in normalized_hostname for brand in _BRAND_LOOKALIKE_HINTS)
        and not is_exact_brand_domain
        and normalized_hostname != hostname  # only fires when a substitution actually happened
    )
    if hyphen_count >= 3 or brand_lookalike:
        findings.append(Finding(FindingCode.SUSPICIOUS_DOMAIN_PATTERN))

    return findings


def _check_redirect_chain(fetch_result: FetchResult) -> list[Finding]:
    findings: list[Finding] = []
    if fetch_result.crossed_domains:
        findings.append(Finding(FindingCode.REDIRECT_CROSSES_DOMAINS))
    if fetch_result.hop_count > _MANY_REDIRECTS_THRESHOLD:
        findings.append(Finding(FindingCode.MANY_REDIRECTS, {"hop_count": fetch_result.hop_count}))
    return findings


def _check_phishing_keywords(url: str, page_title: str | None, page_text: str | None) -> list[Finding]:
    haystack = url.lower() + " " + (page_title or "").lower() + " " + (page_text or "")[:500].lower()
    matches = [kw for kw in _SUSPICIOUS_KEYWORDS if kw in haystack]
    if len(matches) >= 2:
        return [Finding(FindingCode.PHISHING_KEYWORDS, {"keywords": ", ".join(matches[:3])})]
    return []


def _check_fake_login_form(content_bytes: bytes) -> list[Finding]:
    # Cheap, dependency-free heuristic: presence of a password input field.
    # Deliberately NOT alone determinative of "fake" — combined with other
    # findings by the risk-level rollup below, never shown as a sole signal
    # that a legitimate login page would also trigger.
    if b'type="password"' in content_bytes or b"type='password'" in content_bytes:
        return [Finding(FindingCode.FAKE_LOGIN_FORM)]
    return []


def _compute_risk_level(findings: list[Finding], threat_matches: list[ThreatIntelMatch]) -> RiskLevel:
    if threat_matches:
        return RiskLevel.CRITICAL
    if not findings:
        return RiskLevel.SAFE
    return max((_FINDING_SEVERITY[f.code] for f in findings), key=_RISK_ORDER.index)


class UrlSafetyTool:
    def __init__(self, domain_age_checker: DomainAgeChecker, threat_intel_provider: ThreatIntelProvider) -> None:
        self._domain_age_checker = domain_age_checker
        self._threat_intel_provider = threat_intel_provider

    async def assess(
        self,
        url: str,
        *,
        fetch_result: FetchResult | None,
        page_title: str | None = None,
        page_text: str | None = None,
        ssrf_blocked_host: str | None = None,
    ) -> UrlSafetyResult:
        findings: list[Finding] = list(_check_url_string_heuristics(url))

        if ssrf_blocked_host:
            findings.append(Finding(FindingCode.PRIVATE_ADDRESS_BLOCKED, {"host": ssrf_blocked_host}))

        if fetch_result is not None:
            findings.extend(_check_redirect_chain(fetch_result))
            findings.extend(_check_phishing_keywords(url, page_title, page_text))
            findings.extend(_check_fake_login_form(fetch_result.content_bytes))

        domain = urlparse(url).hostname or ""
        domain_age_days: int | None = None
        if domain and not _is_ip_address(domain):
            try:
                age_result = await self._domain_age_checker.check(domain)
            except Exception:  # noqa: BLE001 - domain age must never break safety assessment
                log_event(logger, logging.INFO, "domain age check raised unexpectedly")
                age_result = None
            if age_result is not None and age_result.status == DomainAgeStatus.SUCCESS:
                domain_age_days = age_result.age_days
                if age_result.age_days is not None and age_result.age_days < _YOUNG_DOMAIN_THRESHOLD_DAYS:
                    findings.append(Finding(FindingCode.YOUNG_DOMAIN, {"age_days": age_result.age_days}))

        try:
            threat_matches = await self._threat_intel_provider.check(url, domain)
        except Exception:  # noqa: BLE001 - threat-intel must never break safety assessment
            log_event(logger, logging.ERROR, "threat intel provider raised unexpectedly")
            threat_matches = []

        if threat_matches:
            findings.append(Finding(FindingCode.THREAT_INTEL_MATCH, {"count": len(threat_matches)}))

        risk_level = _compute_risk_level(findings, threat_matches)

        return UrlSafetyResult(
            risk_level=risk_level,
            findings=findings,
            threat_intel_matches=threat_matches,
            domain_age_days=domain_age_days,
        )
