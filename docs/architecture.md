# SathyaScan — System Architecture

Source of truth for product behavior: `SathyaScan — Product Requirements.pdf` (v1.0). This document translates that PRD into a concrete engineering architecture. Where the PRD is silent, decisions are marked **[Decision]**; where a question remains open for the user, it's marked **[Open Question]** and cross-referenced in [`risks-and-open-questions.md`](risks-and-open-questions.md).

**All product/architecture decisions the user has explicitly locked in are recorded in [`decisions.md`](decisions.md) — that document is authoritative if it ever appears to conflict with wording elsewhere in this file.**

## 1. Guiding constraint: the webhook sync/async boundary

Meta's WhatsApp Cloud API requires webhook receivers to respond quickly (undelivered or slow responses trigger retries). SathyaScan's AI analysis pipelines take anywhere from ~5s (text) to 60-90s+ (video, V2/V3). These two facts collide, and resolving that collision is the single most important architectural decision in the system.

**Resolution**: the webhook handler does the absolute minimum synchronously, then hands off.

```
WhatsApp User
   │  message
   ▼
Meta WhatsApp Business Platform
   │  webhook POST (must get 200 OK fast)
   ▼
┌────────────────────────────────────────────────────────────┐
│ SYNCHRONOUS ZONE — FastAPI webhook receiver                 │
│  1. Verify X-Hub-Signature-256 (HMAC-SHA256 over raw body)  │
│  2. Idempotency check on wamid (dedupe Meta retries)        │
│  3. Parse message envelope (type, sender, media_id)         │
│  4. Enqueue job onto Celery/Redis                            │
│  5. Return 200 OK   — target <1-2s, hard ceiling ~5s         │
└────────────────────────────────────────────────────────────┘
   │  enqueue
   ▼
┌────────────────────────────────────────────────────────────┐
│ ASYNC ZONE — Celery workers                                  │
│  Task 1 (near-instant): send "🔍 Analyzing..." ack           │
│  Task 2: download media (Meta media_id → blob storage)       │
│  Task 2.5: SAFETY GATE — prohibited-content check            │
│           (see §5 below; runs before ANY media analysis;     │
│            short-circuits the pipeline on a match)           │
│  Task 3: run SathyaScan Agent orchestrator                   │
│           → deterministic pipeline dispatch by content type  │
│           → per-analysis budget guard (forces                │
│             "insufficient_evidence" on limit hit — distinct  │
│             from "unverified", see §5 / decisions.md §1A)    │
│           → AI Tool Layer                                    │
│           → Evidence Layer (search + source validation)      │
│           → Result Engine (claim-level scoring/fusion)       │
│           → Language Engine (translate to user's language)   │
│           → History Manager (persist, if privacy mode allows)│
│  Task 4: send final WhatsApp reply                            │
└────────────────────────────────────────────────────────────┘
   Note: no "Scheduler / check tomorrow" stage — cut from MVP,
   see decisions.md §4/§14.
   │
   ▼
PostgreSQL  ◄──  Blob Storage (media, PDFs)  ◄──  Redis (session state, queue, rate limits)
   ▲
   │  REST API
Web Dashboard (React)  ◄──  FastAPI Dashboard API (separate router, JWT auth)
```

## 2. Component decisions

| Concern | Decision | Rationale |
|---|---|---|
| Web framework | FastAPI (per PRD) | Async-native, fits the sync/async split above |
| Task queue | **Celery + Redis** | Matches Python stack, mature retry/scheduling story, low ops overhead for MVP scale. See [risks-and-open-questions.md](risks-and-open-questions.md#scheduling-infrastructure) — status: locked, no open question remaining |
| Queue partitioning | `default` (text/URL), `media` (image/OCR), `heavy` (video/audio, V2/V3) | A burst of video jobs must not starve simple text fact-checks |
| Session/conversation state | **Redis, TTL-based** (not Postgres) | High-churn, doesn't need durability; keyed by `phone_number_hash` |
| Reasoning/evidence core | **Claude (Anthropic API)** + dedicated web-search API | Confirmed with user — see [agent-architecture.md](agent-architecture.md) |
| AI-media forensics (MVP) | **Third-party detection APIs** (e.g. Hive Moderation, Sightengine, Reality Defender) | Confirmed with user — avoids self-hosting the hardest, most adversarial ML problem for MVP |
| Infra scale target | **Lean/bootstrapped** — small managed Postgres + Redis, no dedicated GPU fleet | Confirmed with user; revisit at real volume |
| Deployment topology | Single FastAPI app (two routers: webhook + dashboard API) for MVP | Split into separate deployables once traffic justifies decoupling webhook uptime from dashboard issues |
| Cost/abuse limits | **Two-tier, fully config-driven**: per-analysis caps (search count, tool-call count) + global caps (daily spend circuit breaker, per-user rate limit, global concurrency) | Locked decision — [decisions.md §1, §12](decisions.md#1-llm--evidence-search-budget); no hard-coded monetary values anywhere in code |
| Prohibited-content handling | **Safety gate stage**, positioned before any media-analysis tool runs | Locked decision — [decisions.md §6](decisions.md#6-illegal--abusive-content-incl-csam); interface/position locked, vendor/procedure pending mandatory legal review — **public exposure blocked until resolved** |
| Scheduled/proactive messaging | **Not built in MVP** | Locked decision — [decisions.md §4](decisions.md#4-whatsapp--mvp-messaging-scope); removes Meta-template dependency from the MVP critical path entirely |

## 3. Layered view (PRD's own framing, made concrete)

```
WhatsApp Layer     → Meta Cloud API, webhook verification, message send/receive
API Layer          → FastAPI: /webhook/whatsapp (public) + /api/v1 (JWT-authed dashboard)
Agent Layer        → SathyaScan Agent orchestrator (deterministic pipeline dispatch
                      + bounded-tool-use LLM steps, budget-guarded — see agent-architecture.md)
Safety Layer       → Prohibited-content gate (before Tool Layer, media only — see §5)
                      + per-analysis/global budget guard (before/during Tool Layer)
Tool Layer         → Text/Image/Video/Audio Analyzer, OCR, URL Analyzer, URL Safety,
                      Evidence Search, Source Validator, Translation, Report Generator,
                      History Manager (each a swappable service class; no Scheduler
                      tool in MVP — see decisions.md §4/§14)
AI Layer           → Claude API, third-party media-forensics APIs, Whisper/STT (V2)
Evidence Layer      → web search API + source_credibility_registry
Database Layer      → PostgreSQL (see database-schema.md) + blob storage + Redis
```

## 4. Cross-cutting requirements baked into the architecture

- **Idempotency**: every inbound WhatsApp message's `wamid` is unique-constrained in `analyses.source_wamid`; retries are detected and skipped before enqueue.
- **Explainability**: every AI Tool Layer output is a typed finding, not free text; the Result Engine's fusion step produces an explicit `contribution_map` showing which evidence source drove which part of the verdict (PRD §28, §39).
- **Privacy separation**: the Explore aggregation path is structurally incapable of joining back to per-user data — see [database-schema.md §Explore](database-schema.md#explore-privacy-safe-aggregation).
- **Never present probabilistic output as certainty** (PRD §39): enforced at the Result Engine / Language Engine boundary — every media-forensics score and claim verdict is rendered through a hedging template, not raw numbers alone.
- **Graceful degradation**: each pipeline stage failure (OCR fails, search API times out, etc.) is caught and surfaces a friendly PRD-specified error state (see [risks-and-open-questions.md](risks-and-open-questions.md) and PRD §38), never a raw stack trace to the user.

## 5. Cost, Abuse & Safety Controls (locked decisions)

These are architectural components now, not just policy notes — see [`decisions.md`](decisions.md) for the full rationale.

- **Safety gate** (prohibited-content check): runs as Task 2.5, strictly before Task 3's media-analysis tools ever touch the file. On a match, the pipeline short-circuits — no AI analysis runs, the match is logged minimally (never the content itself), and the user receives a generic decline message, never an explanation of what was detected or why. **The gate's position and short-circuit behavior are locked; the detection vendor and reporting procedure are explicitly not decided here and are pending mandatory legal/professional review — see [decisions.md §6](decisions.md#6-illegal--abusive-content-incl-csam). The system must not be exposed publicly until that review is complete.**
- **Per-analysis budget guard**: wraps the orchestrator's tool-calling loop (agent-architecture.md). Tracks evidence-search count and LLM/tool-call count against config-driven ceilings (`MAX_EVIDENCE_SEARCHES_PER_ANALYSIS`, `MAX_LLM_TOOL_CALLS_PER_ANALYSIS`). On limit, stops issuing new calls and resolves any still-open claim as `insufficient_evidence` — a first-class, non-definitive result, not an error, and never a signal that the claim leans true or false. **This is a distinct outcome from `unverified`** (investigation completed but evidence too weak) — see [decisions.md §1A](decisions.md#1a-result-status-definitions--unverified-vs-insufficient_evidence) for the full definitions and the invariant tying `investigation_complete`/`incomplete_reason` to which one applies.
- **Global circuit breaker**: a daily spend/usage counter (`DAILY_SPEND_CIRCUIT_BREAKER_USD` or a usage-based proxy) that, once tripped, queues new analyses rather than processing them, and fires an ops alert. Independent of per-user rate limiting and the per-analysis guard above.
- **Concurrency limits**: the existing `heavy`-queue concurrency cap (media) is joined by a separate global concurrency cap on in-flight LLM/search calls, since text-only analyses never touch the `heavy` queue but still consume LLM/search budget.
- **Hard limits, all config-driven**: max upload file size, max per-analysis processing duration (wall-clock timeout → graceful PRD §38 error state), max tool calls, max evidence searches. None of these are hard-coded in application code — see updated `.env.example`.
- **Duplicate-content detection**: `media_attachments.sha256_hash` (already in the schema) plus a text-similarity check for claims are used to detect a repeated viral submission and reuse a recent analysis when safe (matching language, and not crossing a Privacy Mode boundary — see [decisions.md §12](decisions.md#12-abuse-protection)) instead of reprocessing from scratch.
- **Security-relevant additions** (full detail in [decisions.md §15](decisions.md#15-security-release-gate)): SSRF protection on the URL Analyzer/URL Safety Module (resolved-IP allow/blocklisting, not just URL-string checks) and prompt-injection defenses (content extracted from user input, OCR, transcripts, or fetched URLs is always passed to the LLM as untrusted data, never as instructions). Both are release-gate items for Phase 6, not aspirational.

## 6. Trust & Safety layer (Phase 9)

Built on the existing components above rather than as a parallel system — see [`risks-and-open-questions.md`](risks-and-open-questions.md)'s Phase 9 entry for the full implementation status.

- **Admin identity is structurally separate from user identity**: `admin_users` is its own table with no FK relationship to `users` at all (email+password via bcrypt, versus WhatsApp-OTP for end users). Admins are internal operators, a genuinely different identity domain — modelling them as a role flag on `users` would blur exactly the boundary this layer exists to enforce. **No public signup endpoint exists**; rows are created only via `scripts/create_admin.py`, run locally with direct database access.
- **Token-type separation is structural, not conventional**: dashboard and admin JWTs are signed with the same secret but carry a `token_type` claim that each auth dependency checks explicitly. An admin token is rejected by every user route and vice versa — the check does not depend on the two ID spaces never colliding.
- **Two-tier RBAC via distinct dependencies**, not a runtime branch: `require_admin_role` (user management, system internals, audit log) and `require_moderator_or_admin_role` (appeals/moderation review) are separate FastAPI dependencies, so a route's privilege level is visible in its signature rather than buried in an `if` inside the handler.
- **Appeals never mutate a verdict.** An appeal is a request for human review recorded in its own table; approving one records a decision and does not write to `analyses`/`claims`. Automatic reclassification would be a genuinely different (and much riskier) feature — deliberately out of scope, not an oversight.
- **Moderation is a human-review ticketing layer**, downstream of the detection infrastructure that already exists (safety gate, URL-safety heuristics). No new AI moderation system was introduced.
- **Suspension is enforced at both existing auth choke points** — `get_current_user` (dashboard) and the webhook's per-message user lookup (WhatsApp, with an honest decline reply in all four languages). A still-valid token stops working on its next request, no token-registry sweep needed — the same live-row-check mechanism `is_deleted` already established.
- **Observability is in-process and deliberately minimal**: `provider_health` and `job_status` are in-memory per-process registries wired into each Tool's existing try/except and each periodic job's existing loop, exposed admin-only. They answer "is this code path succeeding right now," complementing `/health`'s existing "is a real provider configured at all." No external metrics backend is wired — nothing in this environment could receive or verify one.
