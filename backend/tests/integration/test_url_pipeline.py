"""
Full URL-pipeline scenario tests, exercised directly against UrlPipeline
(mirrors tests/integration/test_image_pipeline.py's structure) so each
scenario can script the fakes precisely. Uses a real in-memory SQLite DB so
persistence (url_safety_scans, claims, evidence) is tested against real
queries, not mocks.
"""

from unittest.mock import patch

import fakeredis.aioredis
from sqlalchemy import select

from app.agent.tools.url_analyzer import UrlAnalyzerTool
from app.agent.tools.url_safety import NullThreatIntelProvider, UrlSafetyTool
from app.agent.url_pipeline import UrlPipeline, UrlPipelineDependencies
from app.agent.user_service import get_or_create_user
from app.core.config import Settings
from app.core.ssrf_guard import SSRFBlockedError
from app.integrations.evidence_search_client import SearchResult
from app.models.claim import Claim
from app.models.url_safety_scan import UrlSafetyScan
from app.web.domain_age import DomainAgeResult, DomainAgeStatus
from app.web.fetcher import FetchResult, UnsafeUrlError, UrlFetchTimeout

from tests.conftest import (
    TEST_PHONE_ENCRYPTION_KEY,
    TEST_PHONE_PEPPER,
    FakeDomainAgeChecker,
    FakeEvidenceSearchProvider,
    FakeLLMClient,
    FakeUrlFetcher,
    make_text_response,
    make_tool_response,
)


class _TurnScript:
    def __init__(self, claims_response, synthesis_response=None, investigation_turns=None):
        self.claims_response = claims_response
        self.synthesis_response = synthesis_response
        self.investigation_turns = list(investigation_turns or [])
        self._i = 0

    def __call__(self, ctx):
        if ctx.forced_tool_name == "record_claims":
            return self.claims_response
        if ctx.forced_tool_name == "record_synthesis":
            return self.synthesis_response
        if self._i < len(self.investigation_turns):
            resp = self.investigation_turns[self._i]
            self._i += 1
            return resp
        return make_text_response()


def _settings(**overrides) -> Settings:
    base = dict(
        WHATSAPP_APP_SECRET="x",
        WHATSAPP_WEBHOOK_VERIFY_TOKEN="x",
        WHATSAPP_ACCESS_TOKEN="x",
        WHATSAPP_PHONE_NUMBER_ID="x",
        PHONE_HASH_PEPPER=TEST_PHONE_PEPPER,
        PHONE_ENCRYPTION_KEY=TEST_PHONE_ENCRYPTION_KEY,
        MAX_EVIDENCE_SEARCHES_PER_ANALYSIS=5,
        MAX_LLM_TOOL_CALLS_PER_ANALYSIS=10,
        DAILY_SPEND_CIRCUIT_BREAKER_USD=50.0,
        GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT=20,
        MIN_CONCURRING_TIER2_SOURCES=2,
        URL_ROBOTS_TXT_CHECK_ENABLED=True,
        MAX_URL_CONTENT_LENGTH_CHARS=20_000,
    )
    base.update(overrides)
    return Settings(**base)


async def _make_user(db_session, phone="15551234567"):
    return await get_or_create_user(db_session, phone, TEST_PHONE_PEPPER, TEST_PHONE_ENCRYPTION_KEY)


def _pipeline(db_session, settings, llm, search, url_fetcher, domain_age_checker=None) -> UrlPipeline:
    deps = UrlPipelineDependencies(
        llm=llm,
        search_provider=search,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        url_fetcher=url_fetcher,
        url_analyzer_tool=UrlAnalyzerTool(),
        url_safety_tool=UrlSafetyTool(
            domain_age_checker or FakeDomainAgeChecker(), NullThreatIntelProvider()
        ),
    )
    return UrlPipeline(db_session, deps, settings)


_HTML_WITH_CLAIM = (
    b"<html><head><title>Article</title></head>"
    b"<body><p>The scheme was announced.</p></body></html>"
)


def _always_safe_hostnames(hostname):
    return ["93.184.216.34"]


class TestFullPipelineWithClaim:
    async def test_url_with_verifiable_claim_produces_both_blocks(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        llm.responder = _TurnScript(
            claims_response=make_tool_response(
                "record_claims", {"claims": [{"text": "The scheme was announced.", "category": "gov_scheme"}]}
            ),
            synthesis_response=make_tool_response(
                "record_synthesis",
                {
                    "evidence_stances": ["supporting"],
                    "suggested_result": "verified",
                    "claim_confidence": 0.9,
                    "reasoning_text": "Confirmed by an official source.",
                },
            ),
            investigation_turns=[make_tool_response("search_evidence", {"query": "scheme announcement"})],
        )
        search = FakeEvidenceSearchProvider(
            responder=lambda q: [SearchResult(title="Official notice", url="https://pib.gov.in/notice", snippet="Confirmed.")]
        )
        url_fetcher = FakeUrlFetcher(
            responder=lambda url: FetchResult(
                final_url=url, status_code=200, content_bytes=_HTML_WITH_CLAIM, content_type="text/html"
            )
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, search, url_fetcher)

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://example.com/article", source_wamid="wamid.u1", language="en"
            )

        assert outcome.analysis.status == "completed"
        assert outcome.analysis.overall_result == "verified"
        assert outcome.analysis.input_type == "url"
        assert outcome.analysis.input_text == "https://example.com/article"  # URL only, never page content
        assert "Link Safety" in outcome.reply_text
        assert "SathyaScan Result" in outcome.reply_text

        claims_result = await db_session.execute(select(Claim).where(Claim.analysis_id == outcome.analysis.id))
        assert len(claims_result.scalars().all()) == 1

        scan_result = await db_session.execute(
            select(UrlSafetyScan).where(UrlSafetyScan.analysis_id == outcome.analysis.id)
        )
        scans = scan_result.scalars().all()
        assert len(scans) == 1
        assert scans[0].risk_level == "safe"


class TestSsrfBlockedUrl:
    async def test_ssrf_blocked_url_is_never_fetched_but_still_gets_safety_result(self, db_session, seeded_registry):
        llm = FakeLLMClient()  # must never be called
        url_fetcher = FakeUrlFetcher()

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher)

        with patch(
            "app.agent.url_pipeline.resolve_and_check_hostname", side_effect=SSRFBlockedError("blocked")
        ):
            outcome = await pipeline.run(
                user=user, url="https://internal.example/secret", source_wamid="wamid.u2", language="en"
            )

        assert url_fetcher.call_count == 0  # never actually fetched
        assert llm.call_log == []  # no claim extraction attempted
        assert outcome.analysis.status == "completed"  # a real deliverable (the safety block) was sent
        assert outcome.analysis.overall_result == "insufficient_evidence"
        assert outcome.analysis.error_code == "unsafe_url"
        assert "private" in outcome.reply_text.lower() or "internal" in outcome.reply_text.lower()

        scan_result = await db_session.execute(
            select(UrlSafetyScan).where(UrlSafetyScan.analysis_id == outcome.analysis.id)
        )
        assert scan_result.scalars().one().risk_level == "critical"


class TestRobotsDisallowed:
    async def test_robots_disallowed_url_is_never_fetched(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        url_fetcher = FakeUrlFetcher()

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher)

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            with patch("app.agent.url_pipeline.is_allowed_by_robots_txt", return_value=False):
                outcome = await pipeline.run(
                    user=user, url="https://example.com/private", source_wamid="wamid.u3", language="en"
                )

        assert url_fetcher.call_count == 0
        assert llm.call_log == []
        assert outcome.analysis.overall_result == "insufficient_evidence"
        assert outcome.analysis.error_code == "robots_disallowed"


class TestFetchFailureModes:
    async def test_fetch_timeout_still_produces_safety_block(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        url_fetcher = FakeUrlFetcher(responder=lambda url: UrlFetchTimeout("timed out"))

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher)

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://slow.example/page", source_wamid="wamid.u4", language="en"
            )

        assert outcome.analysis.overall_result == "insufficient_evidence"
        assert outcome.analysis.error_code == "timeout"
        assert "Link Safety" in outcome.reply_text  # safety block always present

    async def test_mid_redirect_ssrf_block_is_reported(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        url_fetcher = FakeUrlFetcher(
            responder=lambda url: UnsafeUrlError("blocked mid-redirect", blocked_host="internal.example")
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher)

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://redirector.example/go", source_wamid="wamid.u5", language="en"
            )

        assert outcome.analysis.error_code == "unsafe_url"
        scan_result = await db_session.execute(
            select(UrlSafetyScan).where(UrlSafetyScan.analysis_id == outcome.analysis.id)
        )
        assert scan_result.scalars().one().risk_level == "critical"


class TestNoClaimsOnPage:
    async def test_html_with_no_claims_yields_no_claims_detected(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})
        url_fetcher = FakeUrlFetcher(
            responder=lambda url: FetchResult(
                final_url=url,
                status_code=200,
                content_bytes=b"<html><body><p>Just a picture caption.</p></body></html>",
                content_type="text/html",
            )
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher)

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://example.com/photo", source_wamid="wamid.u6", language="en"
            )

        assert outcome.analysis.overall_result == "no_claims_detected"
        assert outcome.analysis.status == "completed"


class TestUnsupportedContentType:
    async def test_non_html_content_type_skips_extraction(self, db_session, seeded_registry):
        llm = FakeLLMClient()  # must never be called — not HTML
        url_fetcher = FakeUrlFetcher(
            responder=lambda url: FetchResult(
                final_url=url, status_code=200, content_bytes=b"%PDF-1.4 fake pdf bytes", content_type="application/pdf"
            )
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher)

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://example.com/file.pdf", source_wamid="wamid.u7", language="en"
            )

        assert llm.call_log == []
        assert outcome.analysis.overall_result == "no_claims_detected"


class TestBudgetExceededBeforeFetch:
    async def test_zero_tool_call_budget_skips_fetch_but_still_runs_safety(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        url_fetcher = FakeUrlFetcher()

        user = await _make_user(db_session)
        pipeline = _pipeline(
            db_session, _settings(MAX_LLM_TOOL_CALLS_PER_ANALYSIS=0), llm, FakeEvidenceSearchProvider(), url_fetcher
        )

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://example.com/article", source_wamid="wamid.u8", language="en"
            )

        assert url_fetcher.call_count == 0  # budget exhausted before the fetch was even attempted
        assert outcome.analysis.error_code == "tool_call_limit"
        assert outcome.analysis.overall_result == "insufficient_evidence"
        assert "Link Safety" in outcome.reply_text  # URL-string-only heuristics still ran


class TestDomainAgeIntegration:
    async def test_young_domain_finding_surfaces_in_reply(self, db_session, seeded_registry):
        llm = FakeLLMClient()
        llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})
        url_fetcher = FakeUrlFetcher(
            responder=lambda url: FetchResult(
                final_url=url,
                status_code=200,
                content_bytes=b"<html><body><p>Nothing checkable.</p></body></html>",
                content_type="text/html",
            )
        )
        young_domain_checker = FakeDomainAgeChecker(
            responder=lambda domain: DomainAgeResult(status=DomainAgeStatus.SUCCESS, age_days=3)
        )

        user = await _make_user(db_session)
        pipeline = _pipeline(
            db_session, _settings(), llm, FakeEvidenceSearchProvider(), url_fetcher, young_domain_checker
        )

        with patch("app.agent.url_pipeline.resolve_and_check_hostname", side_effect=_always_safe_hostnames):
            outcome = await pipeline.run(
                user=user, url="https://brandnew.example/page", source_wamid="wamid.u9", language="en"
            )

        scan_result = await db_session.execute(
            select(UrlSafetyScan).where(UrlSafetyScan.analysis_id == outcome.analysis.id)
        )
        scan = scan_result.scalars().one()
        assert any(r["code"] == "young_domain" for r in scan.reasons)
