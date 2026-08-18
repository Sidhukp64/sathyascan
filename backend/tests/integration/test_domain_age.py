"""
WhoisDomainAgeChecker tests.

Most tests mock the raw socket-level WHOIS query (_raw_whois_query) for
determinism and offline execution. ONE test (marked clearly below) makes a
REAL live WHOIS lookup against a stable, well-known domain — this is the one
Phase 4 sub-feature genuinely testable end-to-end without any credentials
(WHOIS needs network access only), confirmed working during implementation.
If this specific test is flaky in an offline/firewalled CI environment,
that's expected — network-dependent tests are inherently less reliable than
the rest of the (fully offline) suite, which is why it's isolated here
rather than relied on for coverage of the parsing logic itself (that's
covered by the mocked tests below).
"""

from datetime import date
from unittest.mock import patch

import pytest

from app.web.domain_age import DomainAgeStatus, WhoisDomainAgeChecker, _find_registry_referral, _parse_creation_date

_IANA_ORG_REFERRAL = "% IANA WHOIS server\n\ndomain:       ORG\n\nwhois:        whois.publicinterestregistry.org\n"

_REGISTRY_RESPONSE_ISO = "Domain Name: EXAMPLE.ORG\nCreation Date: 2001-01-13T00:12:14Z\nRegistrar: Example Registrar\n"
_REGISTRY_RESPONSE_DASH_FORMAT = "Domain Name: example.co\nCreated On: 15-Aug-2010\n"
_REGISTRY_RESPONSE_NO_DATE = "Domain Name: EXAMPLE.ORG\nRegistrar: Example Registrar\n"


class TestReferralParsing:
    def test_extracts_whois_server_from_iana_response(self):
        assert _find_registry_referral(_IANA_ORG_REFERRAL) == "whois.publicinterestregistry.org"

    def test_missing_referral_returns_none(self):
        assert _find_registry_referral("% no whois field here\n") is None


class TestCreationDateParsing:
    def test_parses_iso_creation_date(self):
        assert _parse_creation_date(_REGISTRY_RESPONSE_ISO) == date(2001, 1, 13)

    def test_parses_dash_format_created_on(self):
        assert _parse_creation_date(_REGISTRY_RESPONSE_DASH_FORMAT) == date(2010, 8, 15)

    def test_missing_date_returns_none(self):
        assert _parse_creation_date(_REGISTRY_RESPONSE_NO_DATE) is None


class TestWhoisDomainAgeCheckerMocked:
    async def test_successful_lookup_computes_age(self):
        def fake_query(server, query, timeout):
            if server == "whois.iana.org":
                return _IANA_ORG_REFERRAL
            return _REGISTRY_RESPONSE_ISO

        checker = WhoisDomainAgeChecker(timeout_seconds=1.0)
        with patch("app.web.domain_age._raw_whois_query", side_effect=fake_query):
            result = await checker.check("example.org")

        assert result.status == DomainAgeStatus.SUCCESS
        assert result.registered_on == date(2001, 1, 13)
        assert result.age_days is not None and result.age_days > 0

    async def test_missing_referral_is_unavailable(self):
        def fake_query(server, query, timeout):
            return "% no whois field\n"

        checker = WhoisDomainAgeChecker(timeout_seconds=1.0)
        with patch("app.web.domain_age._raw_whois_query", side_effect=fake_query):
            result = await checker.check("example.org")

        assert result.status == DomainAgeStatus.UNAVAILABLE

    async def test_unparseable_response_is_unavailable(self):
        def fake_query(server, query, timeout):
            if server == "whois.iana.org":
                return _IANA_ORG_REFERRAL
            return "garbled response with no recognizable date field"

        checker = WhoisDomainAgeChecker(timeout_seconds=1.0)
        with patch("app.web.domain_age._raw_whois_query", side_effect=fake_query):
            result = await checker.check("example.org")

        assert result.status == DomainAgeStatus.UNAVAILABLE

    async def test_socket_error_is_unavailable_never_raises(self):
        def fake_query(server, query, timeout):
            raise OSError("connection refused")

        checker = WhoisDomainAgeChecker(timeout_seconds=1.0)
        with patch("app.web.domain_age._raw_whois_query", side_effect=fake_query):
            result = await checker.check("example.org")

        assert result.status == DomainAgeStatus.UNAVAILABLE

    async def test_single_label_domain_is_unavailable(self):
        checker = WhoisDomainAgeChecker(timeout_seconds=1.0)
        result = await checker.check("localhost")
        assert result.status == DomainAgeStatus.UNAVAILABLE

    async def test_unexpected_exception_never_propagates(self):
        checker = WhoisDomainAgeChecker(timeout_seconds=1.0)
        with patch("app.web.domain_age._raw_whois_query", side_effect=RuntimeError("boom")):
            result = await checker.check("example.org")
        assert result.status == DomainAgeStatus.UNAVAILABLE


@pytest.mark.live_network
class TestLiveWhoisSmokeTest:
    """REAL network call, no mocking — confirms the two-step IANA-referral +
    registry-WHOIS flow genuinely works end-to-end against a stable,
    long-registered domain. Run explicitly; not required for the rest of the
    suite to be meaningful (see module docstring)."""

    async def test_live_lookup_against_wikipedia_org(self):
        checker = WhoisDomainAgeChecker(timeout_seconds=8.0)
        result = await checker.check("wikipedia.org")

        assert result.status == DomainAgeStatus.SUCCESS
        assert result.registered_on is not None
        assert result.registered_on.year <= 2005  # wikipedia.org registered 2001 — long-settled fact
        assert result.age_days is not None and result.age_days > 0
