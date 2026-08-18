# SathyaScan — Demo

A narrated, end-to-end walkthrough of the whole product against a **live server**, requiring **no credentials of any kind**. One command, ~20 seconds.

## Run it

**If `backend/.venv` already exists, there is no setup — just run.** Two commands, from the repository root:

```powershell
# Windows PowerShell  (replace the path with wherever you cloned this repo)
cd C:\Users\kpsid\OneDrive\Desktop\sidhan
.\backend\.venv\Scripts\python.exe demo\run_demo.py
```

```bash
# macOS / Linux
cd ~/sidhan
backend/.venv/bin/python demo/run_demo.py
```

**You do NOT need to activate the venv.** The command calls the venv's Python directly by path, which also avoids PowerShell's `running scripts is disabled on this system` execution-policy error entirely.

**Check readiness without running the demo** — do this ~5 minutes before you present:

```powershell
.\backend\.venv\Scripts\python.exe demo\run_demo.py --check
```

No Meta account, no API keys, no Postgres, no Redis, no Docker. It creates its own throwaway SQLite database, starts the real app, drives it over real HTTP, then shuts down and cleans up.

While it's running, open **http://127.0.0.1:8099/docs** for the interactive API explorer.

If port 8099 is taken (PowerShell): `$env:DEMO_PORT=8100; .\backend\.venv\Scripts\python.exe demo\run_demo.py`

## First-time setup (only if `backend/.venv` does not exist)

```powershell
cd C:\Users\kpsid\OneDrive\Desktop\sidhan\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Use `requirements-dev.txt`, **not** `requirements.txt` — the demo needs `fakeredis`, which is dev-only. The dev file includes the base file, so this one command installs everything. Installing only `requirements.txt` fails preflight with a message saying exactly this.

Note this uses `.\.venv\Scripts\python.exe -m pip` rather than `activate` + `pip`, for the same execution-policy reason.

## What it walks through

| Act | Shows |
|---|---|
| **0** | System health, and which secrets are configured |
| **1** | The core product: a viral WhatsApp claim → the actual reply the user receives, with evidence and sources. Plus signature rejection and idempotency. |
| **2** | Dashboard: OTP login, history, full result detail with evidence tiers, **PDF report in English and Malayalam**, scheduled re-check, analytics, sessions, notifications |
| **3** | Explore — public trending claims, fetched with no auth, structurally anonymised |
| **4** | Trust & safety: appeals, moderation reports, admin review, operational dashboards, account suspension |
| **5** | Security boundaries proven live: RBAC, cross-domain token rejection, IDOR protection |
| **6** | Privacy: full account deletion, cascade verification, audit-log retention |
| **7** | An explicit summary of what is real vs scripted vs blocked |

Two PDFs are written to `demo/` so you can open them and see the real Malayalam glyph rendering.

## What is real, and what is not

This matters more than the demo itself — **nothing here is presented as something it isn't.**

**Genuinely real:** the FastAPI app, routing, middleware, authentication, RBAC, rate limiting, audit logging; a real database created by the real Alembic migrations; the real fact-checking orchestration and the deterministic classification guard; real source-credibility tiering; real PDF generation including the bundled Noto fonts; real retention and deletion logic. Every HTTP request is a real request to a real uvicorn server.

**Scripted (labelled `[SCRIPTED AI]` wherever it appears):** Claude's responses and web-search results. There are no API keys in this environment. The *pipeline* around them is production code — only the model's answers are canned. **Never present this output as live AI.**

**Blocked by external dependencies (labelled `[BLOCKED]`):**

| Blocked | Why |
|---|---|
| Proactive WhatsApp delivery | Meta requirements + approved message template + messaging budget (decisions.md §4/§14) |
| Image / audio / video ingestion | Real Meta credentials needed to download media |
| OCR, speech-to-text, deepfake detection | Provider selection and credentials |
| Content-safety gate | Qualified legal review — a hard public-launch blocker (decisions.md §6) |
| DPDP / defamation / CSAM policy | Qualified human legal review |

The demo never fakes a WhatsApp delivery, never presents a stub as a live provider, and never claims legal compliance.

## How the substitutions work

`demo_app.py` builds the **real** application via `app.main:create_app()` and replaces exactly four things through FastAPI's dependency-override mechanism — the same technique the project's 709-test suite uses:

1. **Claude** → a scripted responder
2. **Web search** → a scripted result set
3. **WhatsApp sender** → an in-memory capture, so the demo can print the exact reply text a real deployment would send
4. **Redis** → `fakeredis` (no Redis binary in this environment)

It also adds one demo-only endpoint, `GET /__demo/replies`, to read back the captured messages. That route exists **only** on the demo harness — the shipped application is untouched, which is why the endpoint-inventory regression test still sees exactly the 50 real routes.

## Presenting it live

- Run it once beforehand; the first run compiles bytecode and is slightly slower.
- The Act 1 WhatsApp reply is the strongest moment — it's the actual product output, formatted exactly as a user would receive it.
- Open `demo/demo-report-ml.pdf` alongside it to show real Indic-script rendering.
- Keep `http://127.0.0.1:8099/docs` open in a browser tab for questions about the API surface.
- If someone asks "is the AI real?" — the honest answer is in Act 7, and it's already on screen.

## Files

| File | Purpose |
|---|---|
| `run_demo.py` | The narrated walkthrough. Start here. |
| `demo_app.py` | The harness: real app + the four documented substitutions |
| `demo.db` | Throwaway SQLite database (regenerated each run; safe to delete) |
| `demo-report-*.pdf` | Generated during the run |

`demo.db` and the generated PDFs are gitignored.

## Related documentation

- `docs/feature-status-matrix.md` — every feature with its exact status
- `docs/api-demo.md` — manual `curl` walkthrough if you'd rather drive it yourself
- `docs/risks-and-open-questions.md` — the complete, honest limitation register
- `docs/production-deployment.md` — what real deployment requires
