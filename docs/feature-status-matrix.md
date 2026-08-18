# SathyaScan — Final Feature Status Matrix

Produced at the end of Phase 10 (final release & verification). **Every planned feature from the original PRD and the Phase 8-10 roadmap appears here — nothing has been silently dropped.**

## Status vocabulary

| Status | Meaning |
|---|---|
| **COMPLETE** | Implemented, automated-tested, no external dependency blocking it |
| **COMPLETE — LIVE VALIDATED** | The above, PLUS verified against the real external service. **No feature in this project currently holds this status** — no live third-party credentials exist in this environment |
| **IMPLEMENTED — EXTERNAL VALIDATION BLOCKED** | Code complete and tested against fakes/mocks; the real external service has never been called. Not a code gap |
| **IMPLEMENTED — HUMAN/LEGAL REVIEW REQUIRED** | Code complete; a non-engineering review gates its use |

**"Smoke-verified" annotation**: features marked *(smoke-verified)* were additionally exercised over **real HTTP against a live uvicorn server** in Phase 10 — real routing, middleware, auth and database, with only Redis faked (no Redis binary in this environment). This is stronger evidence than automated tests alone, but is still **not** live third-party validation and is never presented as such.

## Core fact-checking

| Feature | Implementation | Automated tests | Live validation | External dependency | Remaining action |
|---|---|---|---|---|---|
| Text fact checking | COMPLETE | Yes (`test_text_pipeline.py`, `test_webhook.py`) | Never — fake LLM/search | Anthropic, Tavily | Set `ANTHROPIC_API_KEY`/`SEARCH_API_KEY`, run one real analysis |
| Evidence investigation (bounded agentic loop) | COMPLETE | Yes | Never | Anthropic, Tavily | As above |
| Credibility classification (deterministic guard) | COMPLETE | Yes — the guard independently recomputes and overrides the LLM | N/A (pure Python) | None | None |
| Explanation / reasoning text | COMPLETE | Yes | Never | Anthropic | As above |
| Claim extraction | COMPLETE | Yes | Never | Anthropic | As above |
| Per-analysis budget guard + daily circuit breaker | COMPLETE | Yes | N/A | None | Set real numeric limits from real cost data |

## Image

| Feature | Implementation | Automated tests | Live validation | External dependency | Remaining action |
|---|---|---|---|---|---|
| Image upload / SSRF-protected download | COMPLETE | Yes (`httpx.MockTransport`, real logic) | Never | Meta | Real Meta credentials |
| Magic-byte validation / decompression-bomb guard | COMPLETE | Yes (real Pillow-generated fixtures) | N/A | None | None |
| OCR | IMPLEMENTED — REAL PROVIDER AVAILABLE, NOT BENCHMARK-SELECTED | Interface + `NullOCRProvider` + `ClaudeVisionOCRProvider` (12 tests, scripted LLM) | Never | Anthropic (reuses `ANTHROPIC_API_KEY`) | `OCR_PROVIDER=claude_vision` enables it; **default is still Null**. decisions.md §11's per-language benchmark has NOT been run, so no engine is *locked for production* — accuracy on Malayalam/Tamil is unmeasured |
| OCR per-language benchmark (decisions.md §11) | COMPLETE (tooling) — NEVER RUN (no corpus) | Yes — 26 tests covering CER/WER scoring, Indic NFC/NFD normalisation, failure-vs-accuracy separation, manifest validation | Never | **A labelled Malayalam/Tamil/Hindi/English image corpus — human work, not a credential** | `app/agent/ocr_benchmark.py` + `scripts/run_ocr_benchmark.py`. Source and label a corpus, run once per candidate engine, compare per-language CER. CER is code-point not grapheme based — comparative, not absolute |
| Image claim extraction → fact check | COMPLETE (reuses the text pipeline verbatim) | Yes | Never | Anthropic, Tavily | As above |
| AI-generated image detection | IMPLEMENTED — EXTERNAL VALIDATION BLOCKED | Interface + Null stub tested | Never | No provider wired | Select + credential a provider (decisions.md §2) |

## URL

| Feature | Implementation | Automated tests | Live validation | External dependency | Remaining action |
|---|---|---|---|---|---|
| URL analysis / HTML extraction | COMPLETE | Yes (real logic via MockTransport) | N/A — no credentials needed | None | None |
| Redirect handling (per-hop SSRF re-check) | COMPLETE | Yes | N/A | None | None |
| SSRF protection | COMPLETE | Yes (private-IP rejection proven) | N/A | None | None |
| robots.txt respect | COMPLETE | Yes | N/A | None | None |
| URL safety heuristics | COMPLETE | Yes | N/A | None | None |
| Domain-age (WHOIS) | COMPLETE | Yes (+ one credential-free live test) | Yes — WHOIS is unauthenticated | None | None |
| Threat-intel API | IMPLEMENTED — EXTERNAL VALIDATION BLOCKED | Null stub tested | Never | Paid subscription | Optional by design; credential a provider if wanted |

## Audio & Video

| Feature | Implementation | Automated tests | Live validation | External dependency | Remaining action |
|---|---|---|---|---|---|
| Audio/video upload + validation (PyAV) | COMPLETE | Yes (real PyAV-encoded fixtures) | N/A | None | None |
| Speech-to-text | IMPLEMENTED — EXTERNAL VALIDATION BLOCKED | Real Sarvam client tested via MockTransport | Never | Sarvam AI | Credential + benchmark (decisions.md §11) |
| Video frame extraction + OCR | COMPLETE (extraction) / blocked (OCR, above) | Yes | N/A | None (extraction) | See OCR row |
| Multimodal fusion | COMPLETE | Yes | N/A | None | None |
| Audio/video claim extraction → fact check | COMPLETE (reuses text pipeline) | Yes | Never | Anthropic, Tavily | As above |
| AI-generated audio/video detection | IMPLEMENTED — EXTERNAL VALIDATION BLOCKED | Real Resemble client tested via MockTransport | Never | Resemble AI (real per-second cost) | Credential + deliberate spend approval; confirm response schema against a real call |

## User features

| Feature | Implementation | Automated tests | Live validation | External dependency | Remaining action |
|---|---|---|---|---|---|
| WhatsApp webhook (signature, idempotency, rate limit) | COMPLETE *(smoke-verified: all 5 input types accepted, bad signature 403)* | Yes | Never | Meta | Real Meta credentials |
| Dashboard authentication (JWT + Redis revocation) | COMPLETE *(smoke-verified: real OTP verify → JWT)* | Yes (incl. fail-closed on Redis outage) | Never (fakeredis) | Redis | Run against real Redis |
| OTP login | IMPLEMENTED — EXTERNAL VALIDATION BLOCKED | Yes (fake sender) | Never | Meta + **approved message template** | Template approval (a disclosed decisions.md §4 conflict) |
| Language selection (en/ml/hi/ta) | COMPLETE *(smoke-verified)* | Yes | N/A | None | Native-speaker review of hi/ta first-pass translations |
| History (list/detail/delete/filter/search/sort) | COMPLETE *(smoke-verified incl. filters)* | Yes | N/A | None | None |
| Privacy Mode + retention purge | COMPLETE | Yes (verified against a real test DB) | N/A | None | None |
| Explore (public, structurally anonymized) | COMPLETE *(smoke-verified, unauthenticated)* | Yes | N/A | None | Fuzzy/embedding clustering is a disclosed future improvement |
| PDF reports (en/ml/hi/ta, real Unicode glyphs) | COMPLETE *(smoke-verified: real PDF bytes generated over HTTP)* | Yes (verified via real `pypdf` text extraction) | N/A | None | None |
| Scheduled re-checks ("Check This Tomorrow") | COMPLETE (backend) *(smoke-verified: create → 409 dup → get → cancel)* | Yes | N/A | None for the backend | See proactive-WhatsApp row below |
| Analytics / Overview | COMPLETE *(smoke-verified)* | Yes | N/A | None | None |
| Conversation sessions | COMPLETE *(smoke-verified)* | Yes | N/A | None | None |
| Notifications (in-app) | COMPLETE *(smoke-verified)* | Yes | N/A | None | None |
| **Proactive WhatsApp notifications** | **IMPLEMENTED — EXTERNAL VALIDATION BLOCKED** | Yes — asserts the notification WOULD be generated with correct recipient/channel/payload, and that **no unauthorized send occurs** | **Never — deliberately** | Meta requirements + template approval + cost budget (decisions.md §4/§14) | All three preconditions, independently confirmed. Every `whatsapp`-channel row is written `blocked_by_policy`; the sender is never invoked |
| Account deletion (cascade + anonymize) | COMPLETE *(smoke-verified: correct cascade counts, token dead instantly, user anonymized, audit_log survived)* | Yes (incl. immediate JWT invalidation) | N/A | None | None |

## Trust & Safety (Phase 9)

| Feature | Implementation | Automated tests | Live validation | External dependency | Remaining action |
|---|---|---|---|---|---|
| Admin system (auth, RBAC, user management) | COMPLETE *(smoke-verified: every admin route + all 4 RBAC boundaries over real HTTP)* | Yes (every permission boundary) | N/A | None | Create the first admin via `python -m scripts.create_admin` |
| Appeals (user + admin review, state machine) | COMPLETE *(smoke-verified: create → 409 dup → review → terminal-state 400)* | Yes (ownership, authz, invalid transitions, duplicates) | N/A | None | None |
| Moderation reports (user + admin review) | COMPLETE *(smoke-verified)* | Yes | N/A | None | None |
| Admin audit logging | COMPLETE *(smoke-verified: 14 real entries, no secrets in metadata, survives account deletion)* | Yes (incl. moderator-can't-read-audit-log) | N/A | None | None |
| Provider & job monitoring | COMPLETE *(smoke-verified)* | Yes | N/A | None | Wiring a real metrics backend is optional future work |
| User suspension (dashboard + WhatsApp enforcement) | COMPLETE *(smoke-verified: same token 200 → 403 → 200 across suspend/reactivate)* | Yes (both surfaces) | N/A | None | None |
| **Content-safety gate (CSAM/illegal content)** | **IMPLEMENTED — HUMAN/LEGAL REVIEW REQUIRED** | Yes — the gate is non-bypassable and fails closed, proven by test | N/A | **A legally-reviewed provider** | **HARD PUBLIC-LAUNCH BLOCKER** (decisions.md §6). `NullSafetyProvider` performs no real check |

## Security

Every item below is **COMPLETE** and covered by automated tests; none has an external dependency.

| Feature | Notes |
|---|---|
| JWT security (HS256, revocation, fail-closed on Redis outage) | Plus Phase 9's structural admin/dashboard `token_type` separation |
| OTP security (hashed, expiring, attempt-limited, enumeration-resistant) | One disclosed non-atomic attempt-counter race (low severity, needs real Postgres to test a fix) |
| Authorization / IDOR protection | 404-not-403 on every per-user resource |
| SSRF protection | Including per-redirect-hop re-checking |
| Rate limiting | Per-user, per-IP, per-admin. **Phase 10 fixed a real unhandled-500 bug on Redis outage** — now fails open, deliberately |
| Upload validation | Magic-byte sniffing, size/duration caps, decompression-bomb guards |
| Privacy isolation | Explore/Overview structurally cannot share a query path |
| Secure PDF access | Ownership-checked, 404-not-403, audit-logged |
| Admin authorization | Two-tier RBAC, enforced by distinct dependencies |
| SQL injection | No raw SQL anywhere except two static `SELECT 1` health probes |
| Secrets never logged | Empirically proven by captured-log-output tests |

## Legal & policy status

| Item | Status |
|---|---|
| Defamation / "False" classification liability | **HUMAN/LEGAL REVIEW REQUIRED** (decisions.md §5) — evidence-first wording, raised bar for high-impact categories, and universal appeals access are all TECHNICALLY ADDRESSED |
| Fact-check disclaimers | TECHNICALLY ADDRESSED — present in every reply and every PDF |
| Copyright / evidence sourcing | TECHNICALLY ADDRESSED — licensed search APIs, snippet-only storage, robots.txt respected |
| AI-generated media claims | TECHNICALLY ADDRESSED — always probabilistic, always shown separately from the fact-check, never the sole verdict driver |
| Illegal content / CSAM handling | **HUMAN/LEGAL REVIEW REQUIRED — HARD PUBLIC-LAUNCH BLOCKER** (decisions.md §6) |
| DPDP / data protection | **HUMAN/LEGAL REVIEW REQUIRED** (decisions.md §9). **Compliance is NOT claimed anywhere** — mechanisms (minimization, erasure, encryption, auditability) are TECHNICALLY ADDRESSED |
| WhatsApp / Meta platform policy | **EXTERNAL APPROVAL REQUIRED** — template approval for proactive messaging |
| Third-party API terms | TECHNICALLY ADDRESSED for those wired; each real provider's terms need review at activation |

## Summary

- **COMPLETE**: 38 features
- **IMPLEMENTED — EXTERNAL VALIDATION BLOCKED**: 9 (OCR, image/audio/video forensics, STT, threat-intel, OTP delivery, proactive WhatsApp, plus real-Redis/Postgres verification). OCR now additionally has a real opt-in provider (`claude_vision`) that is *available but not benchmark-selected* — see the Image row
- **IMPLEMENTED — HUMAN/LEGAL REVIEW REQUIRED**: 1 feature (content-safety gate) + 3 legal reviews
- **COMPLETE — LIVE VALIDATED**: 0 — no live credentials have ever existed in this environment, and this is stated plainly rather than blurred
- **Silently dropped**: none
