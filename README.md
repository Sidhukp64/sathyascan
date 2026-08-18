# SathyaScan

AI-powered WhatsApp fact-checking and digital media verification agent. Primary platform: WhatsApp Business Cloud API. Supporting platform: web dashboard.

**Status**: architecture & scaffold only — no pipeline logic implemented yet. See [`docs/phased-plan.md`](docs/phased-plan.md) for the build order, starting at Phase 0.

## Start here

- [`docs/decisions.md`](docs/decisions.md) — **locked product/architecture decisions — authoritative if anything below appears to conflict with it**
- [`docs/architecture.md`](docs/architecture.md) — system architecture, the webhook sync/async boundary, component decisions
- [`docs/database-schema.md`](docs/database-schema.md) — PostgreSQL schema
- [`docs/api-design.md`](docs/api-design.md) — WhatsApp webhook + dashboard REST API
- [`docs/whatsapp-integration.md`](docs/whatsapp-integration.md) — Meta Cloud API integration details
- [`docs/agent-architecture.md`](docs/agent-architecture.md) — the SathyaScan Agent orchestrator and tool contracts
- [`docs/phased-plan.md`](docs/phased-plan.md) — MVP build order, phase-by-phase
- [`docs/risks-and-open-questions.md`](docs/risks-and-open-questions.md) — gaps in the PRD, now cross-referenced against `decisions.md` for what's locked vs. still genuinely open

**Public launch is additionally gated on legal/professional review of illegal-content handling, DPDP compliance, and defamation/classification policy — see the review table at the end of `docs/decisions.md`.**

The original Product Requirements Document is the source of truth for product behavior; these docs are the engineering translation of it.

## Repo layout

```
backend/        FastAPI app: webhook, dashboard API, agent orchestrator, tools, models, workers
dashboard/      React web dashboard
shared/schemas/ OpenAPI-generated shared types between backend and dashboard
infra/          docker-compose, Dockerfiles, deployment config
docs/           architecture documentation (see above)
```

## Local development

Not yet runnable — Phase 0 (see `docs/phased-plan.md`) sets up the `docker-compose.yml` services, FastAPI skeleton, and Alembic migrations.
