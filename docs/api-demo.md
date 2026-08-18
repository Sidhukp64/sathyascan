# SathyaScan — API Demonstration Guide

Phase 10 §10.12. **SathyaScan is backend-only by locked scope decision — no frontend exists and none was built.** This document is the demonstration: a reproducible, copy-pasteable walkthrough of every major capability through the real HTTP API.

## Honest framing — read this first

**Anything below that depends on an external provider will NOT produce real provider output in an uncredentialed environment**, and this guide never presents stub output as live. Specifically:

- Fact-checking (text/image/URL/audio/video) requires `ANTHROPIC_API_KEY` + `SEARCH_API_KEY`. Without them, every analysis honestly resolves to `insufficient_evidence` — that is correct, designed behavior, **not a demo of working fact-checking**.
- OCR, speech-to-text, and AI-generated-media detection are `Null*` stubs unless credentialed — they report `provider_unavailable`, which the reply text states plainly.
- WhatsApp send/receive requires real Meta credentials.
- **Never present any of the above stub output as if it were live provider output.**

What CAN be demonstrated fully, with no credentials at all: authentication, authorization/RBAC, history, Explore, PDF reports, scheduled checks, overview, sessions, notifications, language selection, account deletion, appeals, admin, and moderation — i.e. the entire application surface except the third-party AI/messaging calls themselves.

## Setup

```bash
cd backend
export DATABASE_URL="sqlite+aiosqlite:///./demo.db"   # or a real Postgres URL
python -m alembic upgrade head
python -m uvicorn app.main:app --port 8000
```

A real Redis is needed for rate limiting/OTP/JWT-revocation to behave normally (`docker compose -f infra/docker-compose.yml up -d redis`). Without Redis the app still runs — rate limiting fails open by design (see `app/core/rate_limit.py`) — but OTP login will not work, since OTP cooldown state lives in Redis.

Set at minimum `JWT_SECRET`, `PHONE_HASH_PEPPER`, `PHONE_ENCRYPTION_KEY` (see `docs/production-deployment.md` §2 for generation commands) — without them auth fails closed by design.

## 1-2. Health, and the full API surface

```bash
curl -s localhost:8000/health | jq
curl -s localhost:8000/openapi.json | jq -r '.paths | keys[]'
```

`/docs` serves interactive Swagger UI over the same schema — the most direct way to explore every endpoint.

## 3. Authentication (WhatsApp OTP)

```bash
curl -sX POST localhost:8000/api/v1/auth/link/start \
  -H 'Content-Type: application/json' -d '{"phone_number":"919812345678"}'
```

**With real Meta credentials** the 6-digit code arrives as a WhatsApp message. Without them the send fails at the Graph API call (audited as `otp_send_failed`) — the OTP row is still created, so a developer can read the code from the `otp_verifications` table to continue the walkthrough. That is a local development affordance, **not** a demonstration of working OTP delivery.

```bash
curl -sX POST localhost:8000/api/v1/auth/link/verify \
  -H 'Content-Type: application/json' \
  -d '{"phone_number":"919812345678","otp_code":"123456"}'
# -> {"access_token": "...", ...}
export TOKEN="<access_token>"
```

## 4-8. Fact checking (text / image / URL / audio / video)

All five arrive through the **WhatsApp webhook**, not a REST endpoint — that is the product's actual input surface. With real Meta credentials, send a WhatsApp message to the configured number and observe the two replies (immediate ack, then the result).

Without Meta credentials, POST a signed webhook payload directly (this is exactly what `tests/integration/test_webhook.py` does — see `_sign()` there for the HMAC signature computation):

```bash
BODY='{"entry":[{"changes":[{"value":{"messages":[{"from":"919812345678","id":"wamid.DEMO1","type":"text","text":{"body":"The government announced free laptops for all students."}}]}}]}]}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WHATSAPP_APP_SECRET" -r | cut -d' ' -f1)"
curl -sX POST localhost:8000/webhook/whatsapp \
  -H "X-Hub-Signature-256: $SIG" -H 'Content-Type: application/json' -d "$BODY"
```

Image/audio/video use the same shape with `"type":"image"|"audio"|"video"` and a `media` object — but note the media download itself calls Meta's API, so those genuinely require credentials.

**Then observe the result through the API** (this part works fully, credentials or not):

```bash
curl -s localhost:8000/api/v1/history -H "Authorization: Bearer $TOKEN" | jq
curl -s localhost:8000/api/v1/history/<analysis_id> -H "Authorization: Bearer $TOKEN" | jq
```

## 7-8. Evidence and credibility result

The history detail response above contains the full per-claim structure: `result` (decisions.md §1A's vocabulary), `claim_confidence`, `evidence_strength`, `evidence_tier_met`, `reasoning_text`, and the `evidence[]` array with each source's URL/domain/publisher/stance/credibility tier. **Uncredentialed, expect `insufficient_evidence` with an empty evidence array — that is the honest, designed outcome, not a failure and not a demo of working evidence retrieval.**

## 9. Explore (public — no auth)

```bash
curl -s "localhost:8000/api/v1/explore/claims?sort=trending" | jq
curl -s "localhost:8000/api/v1/explore/claims?category=health&language=en&q=vaccine" | jq
curl -s localhost:8000/api/v1/explore/categories | jq
```

Note there is **no `Authorization` header** — Explore is deliberately public, and reads a table with no FK to `users`/`analyses` at all. It populates from a periodic rollup (default every 600s) over non-privacy-mode analyses, so it stays empty until such analyses exist.

## 10-11. History and PDF reports

```bash
curl -s "localhost:8000/api/v1/history?type=text&sort=oldest&language=en" -H "Authorization: Bearer $TOKEN" | jq
curl -s localhost:8000/api/v1/history/<analysis_id>/report.pdf \
  -H "Authorization: Bearer $TOKEN" -o report.pdf
```

The PDF renders real Malayalam/Tamil/Hindi glyphs when the analysis's language is `ml`/`hi`/`ta` (bundled SIL OFL Noto fonts). Try an IDOR probe with another user's `analysis_id` — it returns **404, not 403**, by design.

## 12. Scheduled re-check ("Check This Tomorrow")

```bash
curl -sX POST localhost:8000/api/v1/scheduled-checks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"source_analysis_id":"<analysis_id>"}'
curl -s localhost:8000/api/v1/scheduled-checks -H "Authorization: Bearer $TOKEN" | jq
```

The field is `source_analysis_id` (not `analysis_id`) — it is nullable/`SET NULL` so a scheduled check can outlive its source analysis being purged. Note the endpoint returns **400 if the analysis has no claims** (e.g. a `no_claims_detected` result): there is genuinely nothing to re-check, and refusing is correct rather than creating an empty scheduled check.

Scheduling, execution, previous-vs-new diffing, and notification *generation* all work. **The proactive WhatsApp send does not, deliberately** — inspect the `notifications` table and every `channel='whatsapp'` row reads `delivery_status='blocked_by_policy'`. That is the decisions.md §4/§14 gate, not a bug.

## 13-16. Overview, sessions, notifications, language

```bash
curl -s localhost:8000/api/v1/overview -H "Authorization: Bearer $TOKEN" | jq
curl -sX POST localhost:8000/api/v1/sessions -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"Election claims"}'
curl -s "localhost:8000/api/v1/notifications?unread_only=true" -H "Authorization: Bearer $TOKEN" | jq
curl -sX PUT localhost:8000/api/v1/settings/language -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"preferred_language":"ml"}'
```

All four documented languages now report `"translated_replies_available": true`.

## 18. Appeals

```bash
curl -sX POST localhost:8000/api/v1/appeals -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"analysis_id":"<analysis_id>","reason_text":"The source cited is outdated."}'
curl -s localhost:8000/api/v1/appeals -H "Authorization: Bearer $TOKEN" | jq
```

A second appeal on the same analysis while one is open returns **409** — duplicate prevention. Cancelling works only while `status == "open"`.

## 20. Moderation

```bash
curl -sX POST localhost:8000/api/v1/moderation/reports -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"report_type":"malicious_url","target_type":"url","target_url":"https://evil.example.com","description":"Phishing page."}'
```

Reporting deliberately does **not** require owning the target. Listing shows only reports *you submitted* — never reports filed against your content (that view is admin-only, to prevent retaliation).

## 19. Admin

```bash
python -m scripts.create_admin --email ops@example.com --role admin   # interactive password prompt
curl -sX POST localhost:8000/api/v1/admin/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"ops@example.com","password":"<password>"}'
export ADMIN_TOKEN="<access_token>"

curl -s localhost:8000/api/v1/admin/users -H "Authorization: Bearer $ADMIN_TOKEN" | jq
curl -s localhost:8000/api/v1/admin/stats/users -H "Authorization: Bearer $ADMIN_TOKEN" | jq
curl -s localhost:8000/api/v1/admin/system/providers -H "Authorization: Bearer $ADMIN_TOKEN" | jq
curl -s localhost:8000/api/v1/admin/audit-log -H "Authorization: Bearer $ADMIN_TOKEN" | jq
curl -sX POST localhost:8000/api/v1/admin/users/<user_id>/suspend \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"reason":"Spam reports confirmed."}'
```

**The security boundaries are the most worthwhile thing to demonstrate here**, and all four fail correctly:

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/admin/users -H "Authorization: Bearer $TOKEN"        # 401 — user token on an admin route
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/history -H "Authorization: Bearer $ADMIN_TOKEN"      # 401 — admin token on a user route
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/admin/users -H "Authorization: Bearer $MOD_TOKEN"    # 403 — moderator on an admin-only route
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/history -H "Authorization: Bearer $TOKEN"            # 403 — after suspension
```

## 17. Account deletion

```bash
curl -sX DELETE localhost:8000/api/v1/account -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"confirmation_phrase":"DELETE"}'
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/history -H "Authorization: Bearer $TOKEN"   # 401 — same token, now dead
```

The token is invalidated immediately, without any token-registry sweep — `is_deleted` is checked on the live user row on every authenticated request.

## Cleanup

```bash
rm -f backend/demo.db
```
