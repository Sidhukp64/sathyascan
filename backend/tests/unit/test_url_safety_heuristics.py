"""
UrlSafetyTool tests — every heuristic is pure/deterministic (no LLM, no
network for the string-only checks), so these run entirely offline. Domain
age and threat intel are injected as fakes.
"""

from app.agent.tools.url_safety import FindingCode, NullThreatIntelProvider, RiskLevel, ThreatIntelMatch, UrlSafetyTool
from app.web.domain_age import DomainAgeResult, DomainAgeStatus
from app.web.fetcher import FetchResult, RedirectHop


class _FakeDomainAgeChecker:
    def __init__(self, result: DomainAgeResult):
        self._result = result

    async def check(self, domain: str) -> DomainAgeResult:
        return self._result


class _FakeThreatIntelProvider:
    def __init__(self, matches: list[ThreatIntelMatch]):
        self._matches = matches

    async def check(self, url: str, domain: str) -> list[ThreatIntelMatch]:
        return self._matches


def _tool(domain_age_result=None, threat_matches=None) -> UrlSafetyTool:
    domain_age = _FakeDomainAgeChecker(domain_age_result or DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE))
    threat_intel = _FakeThreatIntelProvider(threat_matches or [])
    return UrlSafetyTool(domain_age, threat_intel)


class TestCleanUrlIsSafe:
    async def test_clean_https_url_with_no_fetch_is_safe(self):
        tool = _tool()
        result = await tool.assess("https://example.com", fetch_result=None)
        assert result.risk_level == RiskLevel.SAFE
        assert result.findings == []


class TestUrlStringHeuristics:
    async def test_missing_https_is_flagged(self):
        tool = _tool()
        result = await tool.assess("http://example.com", fetch_result=None)
        assert any(f.code == FindingCode.MISSING_HTTPS for f in result.findings)

    async def test_ip_address_host_is_flagged(self):
        tool = _tool()
        result = await tool.assess("http://192.168.1.1/path", fetch_result=None)
        assert any(f.code == FindingCode.IP_ADDRESS_AS_HOST for f in result.findings)

    async def test_punycode_domain_is_flagged(self):
        tool = _tool()
        result = await tool.assess("https://xn--pypal-4ve.com", fetch_result=None)
        assert any(f.code == FindingCode.PUNYCODE_DOMAIN for f in result.findings)

    async def test_excessive_subdomains_is_flagged(self):
        tool = _tool()
        result = await tool.assess("https://a.b.c.d.example.com", fetch_result=None)
        assert any(f.code == FindingCode.EXCESSIVE_SUBDOMAINS for f in result.findings)

    async def test_many_hyphens_is_flagged_as_suspicious_pattern(self):
        tool = _tool()
        result = await tool.assess("https://secure-login-verify-account.example", fetch_result=None)
        assert any(f.code == FindingCode.SUSPICIOUS_DOMAIN_PATTERN for f in result.findings)

    async def test_leetspeak_brand_lookalike_is_flagged(self):
        tool = _tool()
        result = await tool.assess("https://paypa1.com", fetch_result=None)
        assert any(f.code == FindingCode.SUSPICIOUS_DOMAIN_PATTERN for f in result.findings)

    async def test_real_brand_domain_is_not_flagged_as_lookalike(self):
        tool = _tool()
        result = await tool.assess("https://paypal.com", fetch_result=None)
        assert not any(f.code == FindingCode.SUSPICIOUS_DOMAIN_PATTERN for f in result.findings)


class TestSsrfBlockedFinding:
    async def test_ssrf_blocked_host_yields_critical_finding(self):
        tool = _tool()
        result = await tool.assess(
            "https://internal.example", fetch_result=None, ssrf_blocked_host="internal.example"
        )
        assert result.risk_level == RiskLevel.CRITICAL
        assert any(f.code == FindingCode.PRIVATE_ADDRESS_BLOCKED for f in result.findings)


class TestRedirectChainHeuristics:
    async def test_cross_domain_redirect_is_flagged(self):
        tool = _tool()
        fetch_result = FetchResult(
            final_url="https://other.example/landing",
            status_code=200,
            content_bytes=b"<html></html>",
            content_type="text/html",
            redirect_chain=[RedirectHop(url="https://example.com/link", status_code=302)],
        )
        result = await tool.assess("https://example.com/link", fetch_result=fetch_result)
        assert any(f.code == FindingCode.REDIRECT_CROSSES_DOMAINS for f in result.findings)

    async def test_many_redirects_is_flagged(self):
        tool = _tool()
        fetch_result = FetchResult(
            final_url="https://example.com/final",
            status_code=200,
            content_bytes=b"<html></html>",
            content_type="text/html",
            redirect_chain=[
                RedirectHop(url="https://example.com/1", status_code=302),
                RedirectHop(url="https://example.com/2", status_code=302),
                RedirectHop(url="https://example.com/3", status_code=302),
            ],
        )
        result = await tool.assess("https://example.com/1", fetch_result=fetch_result)
        assert any(f.code == FindingCode.MANY_REDIRECTS for f in result.findings)

    async def test_same_domain_single_redirect_is_not_flagged(self):
        tool = _tool()
        fetch_result = FetchResult(
            final_url="https://example.com/final",
            status_code=200,
            content_bytes=b"<html></html>",
            content_type="text/html",
            redirect_chain=[RedirectHop(url="https://example.com/1", status_code=301)],
        )
        result = await tool.assess("https://example.com/1", fetch_result=fetch_result)
        assert not any(f.code == FindingCode.REDIRECT_CROSSES_DOMAINS for f in result.findings)
        assert not any(f.code == FindingCode.MANY_REDIRECTS for f in result.findings)


class TestPhishingKeywordsAndFakeLoginForm:
    async def test_multiple_phishing_keywords_in_page_are_flagged(self):
        tool = _tool()
        fetch_result = FetchResult(
            final_url="https://example.com",
            status_code=200,
            content_bytes=b"<html></html>",
            content_type="text/html",
        )
        result = await tool.assess(
            "https://example.com",
            fetch_result=fetch_result,
            page_title="Urgent: Verify Your Account Now",
            page_text="Please confirm your account urgently to avoid suspension.",
        )
        assert any(f.code == FindingCode.PHISHING_KEYWORDS for f in result.findings)

    async def test_single_keyword_is_not_enough_to_flag(self):
        tool = _tool()
        fetch_result = FetchResult(
            final_url="https://example.com", status_code=200, content_bytes=b"<html></html>", content_type="text/html"
        )
        result = await tool.assess(
            "https://example.com", fetch_result=fetch_result, page_title="Login page", page_text="just a login"
        )
        assert not any(f.code == FindingCode.PHISHING_KEYWORDS for f in result.findings)

    async def test_password_field_is_flagged(self):
        tool = _tool()
        fetch_result = FetchResult(
            final_url="https://example.com",
            status_code=200,
            content_bytes=b'<html><body><input type="password"></body></html>',
            content_type="text/html",
        )
        result = await tool.assess("https://example.com", fetch_result=fetch_result)
        assert any(f.code == FindingCode.FAKE_LOGIN_FORM for f in result.findings)


class TestDomainAge:
    async def test_young_domain_is_flagged(self):
        tool = _tool(domain_age_result=DomainAgeResult(status=DomainAgeStatus.SUCCESS, age_days=5))
        result = await tool.assess("https://example.com", fetch_result=None)
        assert any(f.code == FindingCode.YOUNG_DOMAIN for f in result.findings)
        assert result.domain_age_days == 5

    async def test_old_domain_is_not_flagged(self):
        tool = _tool(domain_age_result=DomainAgeResult(status=DomainAgeStatus.SUCCESS, age_days=9000))
        result = await tool.assess("https://example.com", fetch_result=None)
        assert not any(f.code == FindingCode.YOUNG_DOMAIN for f in result.findings)
        assert result.domain_age_days == 9000

    async def test_unavailable_domain_age_never_blocks_assessment(self):
        tool = _tool(domain_age_result=DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE))
        result = await tool.assess("https://example.com", fetch_result=None)
        assert result.risk_level == RiskLevel.SAFE
        assert result.domain_age_days is None


class TestThreatIntel:
    async def test_threat_intel_match_forces_critical(self):
        tool = _tool(threat_matches=[ThreatIntelMatch(list_name="test-list", detail="known phishing")])
        result = await tool.assess("https://example.com", fetch_result=None)
        assert result.risk_level == RiskLevel.CRITICAL
        assert any(f.code == FindingCode.THREAT_INTEL_MATCH for f in result.findings)
        assert len(result.threat_intel_matches) == 1

    async def test_null_provider_never_matches(self):
        from app.web.domain_age import DomainAgeResult, DomainAgeStatus

        class _FakeDomainAge:
            async def check(self, domain: str) -> DomainAgeResult:
                return DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)

        tool = UrlSafetyTool(_FakeDomainAge(), NullThreatIntelProvider())
        result = await tool.assess("https://example.com", fetch_result=None)
        assert result.threat_intel_matches == []


class TestProviderFailuresNeverBreakAssessment:
    async def test_domain_age_provider_raising_is_swallowed(self):
        class _RaisingDomainAge:
            async def check(self, domain: str) -> DomainAgeResult:
                raise RuntimeError("simulated WHOIS failure")

        tool = UrlSafetyTool(_RaisingDomainAge(), NullThreatIntelProvider())
        result = await tool.assess("https://example.com", fetch_result=None)
        assert result.risk_level == RiskLevel.SAFE  # degrades gracefully, no crash

    async def test_threat_intel_provider_raising_is_swallowed(self):
        class _RaisingThreatIntel:
            async def check(self, url: str, domain: str) -> list[ThreatIntelMatch]:
                raise RuntimeError("simulated provider failure")

        tool = UrlSafetyTool(
            _FakeDomainAgeChecker(DomainAgeResult(status=DomainAgeStatus.UNAVAILABLE)), _RaisingThreatIntel()
        )
        result = await tool.assess("https://example.com", fetch_result=None)
        assert result.risk_level == RiskLevel.SAFE
        assert result.threat_intel_matches == []
