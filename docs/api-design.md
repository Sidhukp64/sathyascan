# SathyaScan — API Design

**Locked decisions affecting this API are in [`decisions.md`](decisions.md).** Notably: Scheduled Checks routes are V2-only, not built in MVP (§4/§14); no response payload anywhere ever includes a raw or decrypted phone number (§7); Explore/Overview separation below is a security-relevant control, not just a privacy nicety (§15 SSRF/access-control gate applies to the whole API surface).

## Two identity domains

The PRD describes both a WhatsApp-native experience (no login — identity is the phone number) and a web dashboard (§30-32, needs Overview/Explore/History/Reports/Scheduled Checks/Settings/Account) but never states how the two identities map to each other. This is a real product gap the PRD leaves open, resolved here:

**[Decision] WhatsApp-OTP-based linking**:
1. User visits the dashboard and enters their WhatsApp phone number.
2. Backend sends a 6-digit OTP **as a WhatsApp message** to that number — proves phone ownership without needing a separate SMS provider, and reuses the Cloud API integration that already exists.
3. User enters the OTP on the dashboard → backend verifies → creates or links a `dashboard_accounts` row to the corresponding `users` row → issues a JWT.
4. Subsequent dashboard logins reuse the same OTP-via-WhatsApp flow (passwordless — no password-reset UX debt to build or secure). Email/password is deliberately **not** built for MVP; add only if user research shows a real need for a persistent session independent of WhatsApp access.

## Webhook routes (Meta-facing, public)

```
GET  /webhook/whatsapp        Meta verification handshake — echoes hub.challenge
                               after validating hub.verify_token
POST /webhook/whatsapp        Inbound messages/status callbacks.
                               Signature-verified (X-Hub-Signature-256 over raw body)
                               before any parsing. See whatsapp-integration.md.
```

## Dashboard REST API (`/api/v1`, JWT-authenticated except where noted)

```
# Auth
POST /auth/link/start          initiate OTP-based WhatsApp<->dashboard link
POST /auth/link/verify         verify OTP, issue JWT
POST /auth/refresh
POST /auth/logout

# Self
GET  /me                        NEVER includes phone_number_encrypted or a decrypted number
                                 (decisions.md §7) — identifies the user only via user_id/session

# History
GET  /history?type=&result=&from=&to=&q=&page=
GET  /history/{analysis_id}
POST /history/{analysis_id}/reopen
DELETE /history/{analysis_id}
DELETE /history                 clear all (PRD §20: search/filter/sort/reopen/delete/clear)

# Reports (V2)
GET  /reports
POST /reports/{analysis_id}/generate
GET  /reports/{report_id}/download   short-lived signed URL, not a public path

# ============================================================================
# Scheduled checks — "Check This Tomorrow" — NOT BUILT IN MVP.
# Locked decision (decisions.md §4/§14): cut from initial scope entirely, not
# just deferred by default V2 ordering. Do not implement these routes before
# V2 design confirms Meta template approval + cost model. Kept here only as
# the forward route design.
# ----------------------------------------------------------------------------
GET  /scheduled-checks
POST /scheduled-checks
DELETE /scheduled-checks/{id}
# ============================================================================

# Settings
GET  /settings/language
PUT  /settings/language
GET  /settings/privacy
PUT  /settings/privacy          toggling has real side effects — see database-schema.md
                                 Privacy Mode table (triggers retention/deletion jobs)

# Account
DELETE /account                 full erasure — creates a data_deletion_requests row

# Explore — PUBLIC, rate-limited, no auth required
GET  /explore/trending-claims
GET  /explore/topics
GET  /explore/media-categories

# Overview — private, current user's own aggregated stats only
GET  /overview

# Appeals (disagreement with a "False"/"Misleading" verdict — see risks doc)
POST /appeals
GET  /appeals

# Admin — role-gated, scope stays minimal for MVP (decisions.md §13):
# appeals review + source-registry management only, not a general ops UI.
GET  /admin/ops/queue-health
GET  /admin/appeals
POST /admin/appeals/{id}/resolve
GET  /admin/source-registry
PUT  /admin/source-registry/{domain}
```

### As-built route inventory (Phase 9 — supersedes the admin/appeals sketch above)

The sketch above predates implementation. The authoritative, live inventory is the OpenAPI schema itself, guarded by `tests/unit/test_endpoint_inventory.py` (which fails the suite if any route appears that isn't deliberately listed, or if any `/api/v1/*` route ships without `HTTPBearer`). As built:

```
# Appeals — user-facing
POST   /api/v1/appeals
GET    /api/v1/appeals
GET    /api/v1/appeals/{appeal_id}
POST   /api/v1/appeals/{appeal_id}/cancel        # owner-only, and only while status == "open"

# Moderation — user-facing (NOT in the original sketch; new in Phase 9)
POST   /api/v1/moderation/reports                # reporting does NOT require owning the target
GET    /api/v1/moderation/reports                # own SUBMITTED reports only, never reports against you
GET    /api/v1/moderation/reports/{report_id}

# Admin auth — email+password, a separate identity domain from WhatsApp-OTP users
POST   /api/v1/admin/auth/login                  # the only unauthenticated admin route
POST   /api/v1/admin/auth/logout

# Admin — require_admin_role (role == "admin" exactly; a moderator gets 403)
GET    /api/v1/admin/users
GET    /api/v1/admin/users/{user_id}
POST   /api/v1/admin/users/{user_id}/suspend
POST   /api/v1/admin/users/{user_id}/reactivate
GET    /api/v1/admin/stats/users
GET    /api/v1/admin/stats/analyses
GET    /api/v1/admin/system/health
GET    /api/v1/admin/system/jobs                 # background-job status (in-process periodic tasks)
GET    /api/v1/admin/system/providers            # live provider health, not just "is one configured"
GET    /api/v1/admin/audit-log                   # admin-only; a moderator token is 403'd

# Admin — require_moderator_or_admin_role (both roles)
GET    /api/v1/admin/appeals
GET    /api/v1/admin/appeals/{appeal_id}
POST   /api/v1/admin/appeals/{appeal_id}/review  # enforces an explicit state machine
GET    /api/v1/admin/moderation/reports
GET    /api/v1/admin/moderation/reports/{report_id}
POST   /api/v1/admin/moderation/reports/{report_id}/review
```

**Not built** (from the sketch above): `/admin/ops/queue-health` — there is no queue in this architecture (no Celery; three in-process asyncio periodic tasks instead), so `/admin/system/jobs` serves the equivalent purpose against what actually exists. `/admin/source-registry` — the tiered source registry is seeded and read-only in the current design; no editing UI or API was requested or built. Both are recorded here rather than silently dropped.

## Structural privacy enforcement

`/overview` and `/explore/*` must never share a query path. `/overview` reads per-user data scoped to the authenticated JWT's `user_id`. `/explore/*` reads **only** from `explore_claim_clusters` / `explore_daily_rollups`, tables that have no foreign key back to `users`/`analyses` (see `database-schema.md`). Implement this as two physically separate repository/service classes — one that can join to `users` and one that structurally cannot — so PRD §19's "Explore must never expose private user data" is a property of the code path, not a convention a future PR could accidentally violate.

## Security controls applied across this whole surface (decisions.md §15)

- **Rate limiting** on every route, not just `/explore/*` — per-user for authenticated routes, per-IP for public ones (`/explore/*`, `GET /webhook/whatsapp`).
- **Authorization**: dashboard routes are scoped to the JWT's `user_id` (never a client-supplied `user_id` parameter); admin routes require the `admin` role, checked server-side on every request, not just at login.
- **No phone numbers in any response body, query parameter, or URL path** anywhere in this API (decisions.md §7) — a request needing to identify a user does so by `user_id` (opaque UUID) or the authenticated session, never the phone number itself.
- **File-accepting routes** (media arriving via the WhatsApp webhook, not a dashboard upload route in MVP) enforce the max-file-size and safety-gate checks in `decisions.md` §6/§12/§15 before any processing.
- **Prompt-injection isolation**: no API response — including evidence snippets or transcripts that ultimately came from an LLM call — is trusted as executable instruction by any downstream LLM call; see `agent-architecture.md`.

## Response shape (evidence-first, per PRD §15)

Every analysis-result endpoint (`GET /history/{id}`, and the WhatsApp reply payload before translation) returns the same shape, never a bare label. Note the added `detector` block (decisions.md §2 — provider/version must be shown, not just logged), `evidence_tier_met` (decisions.md §3), and `investigation_complete` / `incomplete_reason` (decisions.md §1A):

```json
{
  "analysis_id": "...",
  "input_type": "text",
  "claims": [
    {
      "claim_text": "Government announced ₹50,000 for every student.",
      "result": "false",
      "reasoning_text": "No official government source confirms this announcement.",
      "claim_confidence": 0.91,
      "media_confidence": null,
      "evidence_strength": 0.85,
      "evidence_tier_met": 1,
      "investigation_complete": true,
      "incomplete_reason": null,
      "evidence": [
        {"stance": "contradicting", "source_url": "...", "publisher_name": "...", "credibility_tier": "tier_1_gov_official"}
      ],
      "detector": null
    }
  ],
  "checked_at": "2026-08-12T10:00:00Z",
  "language": "en",
  "budget_limit_hit": false
}
```

For media analyses, `detector` is populated per decisions.md §2, e.g. `{"provider_name": "hive", "model_version": "v3.2", "probability": 0.87, "label": "likely_ai_generated"}` — never a bare probability with no provider/version attached.

**`unverified` vs `insufficient_evidence` — two distinct, non-interchangeable outcomes (decisions.md §1A)**, both always returned with `evidence` and `reasoning_text` populated so the caller can see what was found and why the outcome landed where it did:

```json
// investigation completed; evidence found but too weak/inconsistent to classify
{
  "result": "unverified",
  "reasoning_text": "We found some sources discussing this topic, but they conflict and none meet the evidence bar needed for a confident verdict.",
  "investigation_complete": true,
  "incomplete_reason": null,
  "evidence": [ /* whatever was actually found, however weak */ ]
}

// investigation could NOT be completed — a limit, not a verdict
{
  "result": "insufficient_evidence",
  "reasoning_text": "We weren't able to complete enough research to check this claim fully within the available search budget. This does not mean the claim is true or false.",
  "investigation_complete": false,
  "incomplete_reason": "search_limit",
  "evidence": [ /* partial findings gathered before the cutoff, if any — never used to bias the result */ ]
}
```

`budget_limit_hit` at the top level is analysis-wide (some claims in a multi-claim analysis may have completed fine before the limit tripped); `investigation_complete`/`incomplete_reason` on each claim is the authoritative per-claim record of whether *that specific claim's* investigation finished. WhatsApp-facing text may render both outcomes with similar, non-alarming phrasing (decisions.md §1A), but the API and stored record always keep them distinct, and both `evidence` and `reasoning_text` are mandatory fields on every result, never omitted for either outcome.
