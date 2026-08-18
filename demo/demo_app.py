"""
SathyaScan demo harness — boots the REAL application with demo-safe
substitutions so the whole product can be shown without any paid API key,
Meta account, Postgres, or Redis.

WHAT IS REAL HERE (everything except the four substitutions below):
  * The real FastAPI app, real routing, real middleware, real auth
    dependencies, real RBAC, real rate limiting, real audit logging.
  * A real SQLite database created by the real Alembic migrations.
  * The real fact-checking pipeline: real claim/evidence orchestration, the
    real deterministic classification guard, real source-credibility
    tiering, real PDF generation, real retention/deletion logic.

WHAT IS SUBSTITUTED, AND WHY (each one is a hard external dependency this
environment cannot satisfy — never a shortcut around working code):
  1. Claude  -> a SCRIPTED responder. No ANTHROPIC_API_KEY exists. The
     pipeline around it is entirely real; only the model's answers are
     canned. Output derived from this is labelled "SCRIPTED AI" everywhere
     it appears in the demo, and must never be presented as live AI output.
  2. Web search -> a SCRIPTED result set, for the same reason.
  3. WhatsApp sender -> an in-memory capture, so the demo can SHOW the exact
     reply text that a real Meta deployment would deliver. Nothing is sent.
  4. Redis -> fakeredis, since no Redis binary exists here. Rate limiting,
     JWT revocation and OTP cooldown all still execute their real logic
     against it.

Everything the substitutions feed into is genuinely exercised — this is the
same technique the project's 709-test suite uses, not a mock of the product.
"""

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import fakeredis.aioredis  # noqa: E402

from app.agent.tools.url_safety import NullThreatIntelProvider, UrlSafetyTool  # noqa: E402
from app.agent.tools.url_analyzer import UrlAnalyzerTool  # noqa: E402
from app.integrations.claude_client import (  # noqa: E402
    LLMResponse,
    LLMTextBlock,
    LLMToolUseBlock,
)
from app.integrations.evidence_search_client import SearchResult  # noqa: E402
from app.main import create_app  # noqa: E402
from app.webhook.whatsapp.router import (  # noqa: E402
    get_llm_client,
    get_search_provider,
    get_sender,
    get_url_analyzer_tool,
    get_url_safety_tool,
)

# --------------------------------------------------------------------------
# Scripted stand-ins (see module docstring for why each exists)
# --------------------------------------------------------------------------


class CapturingWhatsAppSender:
    """Captures outbound replies instead of calling Meta. The demo prints
    these verbatim — they are exactly the bytes a real deployment would send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_text_message(self, to_phone: str, phone_hash: str, body: str) -> None:
        self.sent.append((to_phone, phone_hash, body))


def _tool_response(tool_name: str, tool_input: dict) -> LLMResponse:
    return LLMResponse(
        content=[LLMToolUseBlock(tool_use_id=f"toolu_{tool_name}", tool_name=tool_name, tool_input=tool_input)],
        stop_reason="tool_use",
    )


class ScriptedLLM:
    """A canned Claude. Dispatches on which pipeline stage is calling, so the
    real orchestrator/guard logic downstream runs completely unmodified."""

    def __init__(self) -> None:
        self.call_count = 0

    async def create_message(self, *, system, messages, tools=None, tool_choice=None, max_tokens=1024):
        self.call_count += 1
        forced = tool_choice["name"] if tool_choice else None

        if forced == "record_claims":
            return _tool_response(
                "record_claims",
                {"claims": [{"text": "The government announced free laptops for all students.", "category": "gov_scheme"}]},
            )

        if forced == "record_synthesis":
            # Two contradicting sources, one neutral — the deterministic
            # guard in app/agent/classification.py independently re-derives
            # the final label from this evidence and may override it.
            return _tool_response(
                "record_synthesis",
                {
                    "evidence_stances": ["contradicting", "contradicting", "neutral"],
                    "suggested_result": "false",
                    "claim_confidence": 0.88,
                    "reasoning_text": (
                        "The Press Information Bureau's official fact-check unit has explicitly denied this "
                        "announcement, and no government portal lists any such scheme. The message appears to "
                        "be a recycled forward from a previous year."
                    ),
                },
            )

        # Investigation loop turn: request one search, then signal done.
        if tools and self.call_count <= 2:
            return _tool_response("search_evidence", {"query": "government free laptops all students announcement"})
        return LLMResponse(content=[LLMTextBlock(text="Investigation complete.")], stop_reason="end_turn")


class ScriptedSearch:
    """Canned evidence. Domains match the demo's seeded source-credibility
    registry so real tier-based scoring genuinely applies."""

    def __init__(self) -> None:
        self.call_count = 0

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self.call_count += 1
        return [
            SearchResult(
                title="Fact Check: No such laptop scheme has been announced",
                url="https://pib.gov.in/factcheck/laptop-scheme-denial",
                snippet=(
                    "A viral message claiming the government will give free laptops to all students is FAKE. "
                    "No such scheme has been announced by any ministry."
                ),
                published_at="2026-07-14",
            ),
            SearchResult(
                title="Viral laptop scheme message resurfaces, officials deny",
                url="https://reputablenews.example/viral-laptop-claim-denied",
                snippet="Officials confirmed to this publication that no laptop distribution programme exists.",
                published_at="2026-07-15",
            ),
            SearchResult(
                title="State education budget 2026 overview",
                url="https://othernews.example/education-budget-overview",
                snippet="The education budget allocates funds across infrastructure and teacher training.",
                published_at="2026-06-02",
            ),
        ]


# --------------------------------------------------------------------------
# App assembly
# --------------------------------------------------------------------------

sender = CapturingWhatsAppSender()
llm = ScriptedLLM()
search = ScriptedSearch()

app = create_app()
app.dependency_overrides[get_sender] = lambda: sender
app.dependency_overrides[get_llm_client] = lambda: llm
app.dependency_overrides[get_search_provider] = lambda: search
# The URL pipeline's fetcher is left REAL (it needs no credentials); only the
# analyzer/safety tools are re-instantiated here so the demo doesn't depend
# on outbound network access for its tier heuristics.
app.dependency_overrides[get_url_analyzer_tool] = lambda: UrlAnalyzerTool()
app.dependency_overrides[get_url_safety_tool] = lambda: UrlSafetyTool(
    __import__("app.web.domain_age", fromlist=["WhoisDomainAgeChecker"]).WhoisDomainAgeChecker(
        timeout_seconds=3.0
    ),
    NullThreatIntelProvider(),
)

_original_lifespan = app.router.lifespan_context


class _DemoLifespan:
    """Runs the REAL lifespan (engine, provider clients, the three periodic
    background tasks) then swaps Redis for fakeredis."""

    def __init__(self, app):
        self._app = app
        self._inner = _original_lifespan(app)

    async def __aenter__(self):
        result = await self._inner.__aenter__()
        self._app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # Expose the scripted doubles so the demo script can read captured
        # replies and call counts back out.
        self._app.state.demo_sender = sender
        self._app.state.demo_llm = llm
        self._app.state.demo_search = search
        return result

    async def __aexit__(self, *args):
        return await self._inner.__aexit__(*args)


app.router.lifespan_context = _DemoLifespan


# --------------------------------------------------------------------------
# Demo-only introspection. Lives ONLY on this harness app — the shipped
# application (app.main:create_app) is untouched, so the endpoint-inventory
# regression test still sees exactly the 50 real routes and nothing here.
# --------------------------------------------------------------------------


@app.get("/__demo/replies", include_in_schema=False)
async def _demo_replies():
    """The outbound WhatsApp messages a real Meta deployment would have sent.
    Captured, never delivered."""
    return {"replies": [body for (_to, _hash, body) in sender.sent]}
