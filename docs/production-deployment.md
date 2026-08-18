# SathyaScan — Production Deployment Guide

Written Phase 9 (roadmap §9.11), verified against the actual `backend/app/core/config.py` Settings fields and `infra/docker/Dockerfile.api`/`infra/docker-compose.yml` — not aspirational. `docker compose config` could not be run to lint the compose file (no `docker` binary available in this sandbox); the two real bugs found by direct inspection (see below) were fixed by hand and should be re-verified with a real `docker compose config`/`docker compose up` before first production use.

**This document describes configuration, not a completed live deployment.** No production environment has ever been provisioned or tested against in this project's history — see `docs/risks-and-open-questions.md`'s Phase 9 §9.7/§9.8/§9.10 entry for the full, honest status of what remains environment-blocked.

## 1. Infrastructure components

| Component | Role | Notes |
|---|---|---|
| PostgreSQL | Primary datastore | `pgvector/pgvector:pg16` in `infra/docker-compose.yml` (the `pgvector` extension is bundled for a future embedding-based feature — not currently used by any query in this codebase; a plain `postgres:16` image works identically today) |
| Redis | Rate limiting, JWT revocation, OTP cooldown/idempotency, notification/appeal in-process state is NOT here (all in Postgres) | No persistence tuning required — every key this app writes carries its own TTL (see `app/core/rate_limit.py`, `app/core/jwt_auth.py`, `app/core/otp_cooldown.py`); a Redis restart only means rate limits/cooldowns/revocations reset, never data loss of anything durable |
| `api` (this backend) | FastAPI app, ASGI (uvicorn) | Also runs three in-process periodic background tasks (see §5 below) — no separate worker process exists in this architecture |
| Object/file storage | **Not used** | This project has never persisted media blobs or generated PDFs to disk/blob storage — media is processed in-memory and discarded (Phase 3+), PDF reports are generated on-demand and streamed (Phase 8) |

## 2. Environment variables

Every variable below maps 1:1 to a `Settings` field in `backend/app/core/config.py` — that file is the authoritative source; this table is a deployment-oriented summary, not a duplicate spec. See the repo-root `.env.example` for the full list with default values — verified complete in Phase 10: all 89 `Settings` fields appear there (9 Phase 8/9 fields were found missing during that audit and added).

**Must be set for the app to be usable at all** (each has a documented, honest fail-closed behavior when empty — see `GET /health` / `GET /api/v1/admin/system/health`, both of which report these as booleans without ever echoing the actual value):

- `DATABASE_URL` — e.g. `postgresql+asyncpg://sathyascan:sathyascan@postgres:5432/sathyascan` (inside docker-compose, the hostname must be the service name `postgres`, never `localhost`)
- `REDIS_URL` — e.g. `redis://redis:6379/0` (same hostname rule)
- `JWT_SECRET` — signs BOTH dashboard and admin tokens (a `token_type` claim distinguishes them, not a second secret — see `app/core/jwt_auth.py`); generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `PHONE_HASH_PEPPER` / `PHONE_ENCRYPTION_KEY` — decisions.md §7; generate the encryption key with `python -c "from app.core.encryption import generate_encryption_key as g; print(g())"`
- `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` — required for any real WhatsApp message to be received/sent at all
- `ANTHROPIC_API_KEY`, `SEARCH_API_KEY` — required for any real fact-check to complete (without them every analysis resolves to `insufficient_evidence`, never a crash — see `app/agent/investigation_loop.py`)

**Required before the system may be exposed publicly** (decisions.md §6, a hard launch gate, not a config nicety):

- `SAFETY_GATE_PROVIDER` — must point at a real, legally-reviewed content-safety provider; `NullSafetyProvider` (the default when unset) is a passthrough stub that performs no real check at all

**Optional, opt-in real providers** (each defaults to a `Null*` stub if unset — see `docs/risks-and-open-questions.md` for the full credentialing/benchmarking status of each):

- `SPEECH_TO_TEXT_PROVIDER=sarvam` + `SPEECH_TO_TEXT_API_KEY`
- `AUDIO_FORENSICS_PROVIDER=resemble` + `AUDIO_FORENSICS_API_KEY`
- `VIDEO_FORENSICS_PROVIDER=resemble` + `VIDEO_FORENSICS_API_KEY`

**First admin account**: there is no environment variable or public endpoint for this — run `python -m scripts.create_admin --email you@example.com --role admin` from inside the `api` container (or any environment with the same `DATABASE_URL`) once, interactively, after the first migration.

## 3. Secrets management

- **Never commit `.env`** — `.gitignore` already excludes both `.env` and `.env.*` (confirmed present); the repo-root `.env.example` holds only placeholder values, never real secrets.
- In a real deployment, prefer a managed secrets store (AWS Secrets Manager / GCP Secret Manager / HashiCorp Vault / your platform's native secret injection) over a plain `.env` file on disk — `docker-compose.yml`'s `env_file: ../.env` is a local-dev convenience, not a production secret-delivery mechanism.
- Every secret-bearing config field (`JWT_SECRET`, `PHONE_HASH_PEPPER`, `PHONE_ENCRYPTION_KEY`, every `*_API_KEY`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, admin passwords) is confirmed never logged anywhere in this codebase (a standing discipline verified by dedicated tests — `tests/unit/test_provider_credential_safety.py` and others — throughout every phase, re-confirmed present in Phase 9's cleanup audit).

## 4. HTTPS / CORS / networking

- **HTTPS termination is expected to happen in front of this app** (a load balancer, reverse proxy, or platform-managed TLS) — the FastAPI app itself serves plain HTTP; nothing in this codebase implements TLS termination, and none should be added here (that's an infra-layer concern, not an application one).
- **No CORS middleware exists, deliberately** (`app/core/http_hardening.py`'s docstring) — this backend has no frontend (a confirmed, locked scope decision), so the browser's default same-origin restriction is already the correct posture. A future first-party frontend or third-party integration would need CORS added deliberately and narrowly at that time, never a wildcard `allow_origins=["*"]`.
- `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` are set on every response; `Cache-Control: no-store` on every `/api/v1/*` response (`SecurityHeadersMiddleware`, Phase 7).

## 5. Background jobs / scheduled tasks

Three periodic jobs run **in-process**, inside the same `api` ASGI process, started/cancelled by `app/main.py`'s `lifespan()`:

| Job | Interval config | Purpose |
|---|---|---|
| Retention purge | `RETENTION_PURGE_INTERVAL_SECONDS` (default 300s) | Privacy Mode media/analysis expiry (decisions.md §8) |
| Scheduled-check runner | `SCHEDULED_CHECK_EXECUTION_INTERVAL_SECONDS` (default 300s) | "Check This Tomorrow" re-checks (decisions.md §14) |
| Explore rollup | `EXPLORE_ROLLUP_INTERVAL_SECONDS` (default 600s) | Public Explore aggregation |

**No Celery, no separate worker process, no external scheduler** — a deliberate architectural choice made in Phase 6 and held consistently since (see `docs/phased-plan.md`'s sequencing rationale). This has one real production implication worth planning for before horizontally scaling: **running more than one `api` replica means each of these three jobs fires once per replica, per interval** — harmless for the retention purge and Explore rollup (both idempotent — re-running them early or twice does no additional work, just wasted cycles), but the scheduled-check runner should either be pinned to exactly one replica, or the periodic-task loops should be moved behind a distributed lock (e.g. a Redis `SET NX` lock per tick) before running multiple `api` replicas in production. **Not built in this codebase** — flagged here for whoever provisions horizontal scaling, not silently assumed away.

Live job health is visible via `GET /api/v1/admin/system/jobs` (Phase 9, `app/core/job_status.py`) — `last_run_at`/`last_success_at`/`last_error`/`run_count`/`failure_count` per job, admin-only.

## 6. Migrations

- **Never run automatically on `api` container startup** — deliberately (an auto-migrating web process is a common source of multi-replica rolling-deploy races). Run as an explicit, separate step:
  ```bash
  docker compose run --rm api alembic upgrade head
  ```
- Verify the FULL cycle before trusting a migration in production, exactly as every phase of this project has done against SQLite: `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head`, ending back at the same head revision (current head: `0008`). **This has never been run against a real Postgres instance in this project's history** — SQLite's relaxed type/constraint enforcement means a real Postgres run could surface issues SQLite silently accepted; re-verify this cycle against a real staging Postgres before the first production deploy.
- Rollback plan: `alembic downgrade -1` reverts exactly one migration; every migration in `backend/migrations/versions/` has a real, tested (against SQLite) `downgrade()` — but, per the point above, has not been tested against Postgres.

## 7. Logging & monitoring

- Structured logging via `app/core/logging.py`'s `log_event()` — every log line is a single structured event, never raw string interpolation of user content; phone numbers/OTP codes/JWTs/API keys are never logged anywhere (verified by dedicated tests since Phase 1, re-confirmed through Phase 9).
- `LOG_LEVEL` (default `INFO`) controls verbosity; `httpx`'s own logger is explicitly suppressed to `WARNING` (prevents verbose request/header logging that could otherwise leak auth headers at `DEBUG` level).
- **No metrics/tracing backend is wired** (no Prometheus/OpenTelemetry/Datadog integration exists) — `GET /health` (liveness-style, public) and `GET /api/v1/admin/system/{health,jobs,providers}` (richer, admin-only, Phase 9) are the only structured operational signals this codebase currently exposes. Wiring a real metrics backend is real, undone future work, not attempted here since nothing in this sandbox could receive/display those metrics to verify the wiring actually works.

## 8. Health checks

- `GET /health` — public, unauthenticated, safe to point a load balancer's liveness/readiness probe at. Returns `"status": "ok"` only when both DB and Redis respond; `"degraded"` otherwise (never a 5xx for a degraded-but-running process — the route itself never raises).
- `GET /api/v1/admin/system/health` — the same DB/Redis check plus safety-gate/JWT configuration flags, admin-authenticated, intended for an operator dashboard rather than an automated probe.

## 9. Backups

- **PostgreSQL**: standard `pg_dump`/managed-provider automated backups — no application-specific backup logic exists or is needed (this app has no non-Postgres durable state; Redis holds only ephemeral, TTL'd data).
- **Retention/deletion interacts with backup/restore in one way worth knowing**: a user's account-deletion (`DELETE /api/v1/account`) and Privacy Mode's retention purge are real, hard deletes — restoring from a backup taken before either would resurrect data the user (or the system, per their privacy setting) had deleted. This is a real property of "hard delete" being genuinely hard, not a bug; document this in whatever internal backup/restore runbook is eventually written, since it has legal/DPDP-review relevance (see `docs/risks-and-open-questions.md` §9 / decisions.md §9).

## 10. Development / staging / production separation

No environment-specific config files exist beyond `.env` itself — `Settings` (pydantic-settings) reads from environment variables / `.env`, so the SAME codebase runs in any environment purely by swapping the `.env` file / injected environment variables. Recommended (not currently enforced anywhere in code):

- **Development**: this sandbox's pattern — SQLite via `DATABASE_URL=sqlite+aiosqlite:///...`, `fakeredis` is test-only (never use it outside `pytest`), Null provider stubs, no real credentials.
- **Staging**: real Postgres + Redis, real provider credentials but capped/budgeted (`DAILY_SPEND_CIRCUIT_BREAKER_USD` set low), a non-production WhatsApp test number.
- **Production**: real everything, `SAFETY_GATE_PROVIDER` set to a legally-reviewed real provider (hard requirement, decisions.md §6), the three legal reviews (defamation/classification, illegal content, DPDP) completed and documented — see `docs/decisions.md`'s "Summary: Review Requirements" table.
