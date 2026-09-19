# Running SathyaScan as a live WhatsApp bot

Getting to the point where a real person forwards a message on WhatsApp and gets a fact-check back. This is the actual product — everything here is configuration and external setup, not new code.

Check where you stand at any time:

```bash
cd backend
.venv/Scripts/python.exe -m scripts.golive_check
```

It reads your `.env`, never prints secret values, and tells you exactly what happens for each forward type.

## ⚠️ Read this before you publish a number

`decisions.md` §6 is explicit: the content-safety gate is a **passthrough stub**. It performs no real CSAM or illegal-content check. On a WhatsApp number that strangers can message, people can send anything, and this system would download and process it.

- **Controlled demo** — you share the number with specific people you know (evaluators, faculty, teammates). Materially lower risk, reasonable to proceed.
- **Public number** — do not, until a real content-safety provider is configured and reviewed. This is a legal exposure question, not a checkbox.

Everything below assumes the controlled-demo path.

---

## Step 1 — Meta app and credentials

At [developers.facebook.com](https://developers.facebook.com): create an app, add the **WhatsApp** product, and open **WhatsApp → API Setup**.

Four values go into `.env`. **Use these exact variable names** — the code reads these and nothing else, and a wrong name fails silently:

```bash
WHATSAPP_ACCESS_TOKEN=          # API Setup -> temporary or system-user token
WHATSAPP_PHONE_NUMBER_ID=       # API Setup -> "Phone number ID" (NOT the phone number)
WHATSAPP_BUSINESS_ACCOUNT_ID=   # API Setup
WHATSAPP_APP_SECRET=            # App Settings -> Basic -> App Secret
WHATSAPP_WEBHOOK_VERIFY_TOKEN=  # you invent this; any random string
```

Two names people commonly get wrong:

| Wrong | Correct | What breaks |
|---|---|---|
| `META_APP_SECRET` | `WHATSAPP_APP_SECRET` | Every incoming message is rejected with 403 |
| `WHATSAPP_VERIFY_TOKEN` | `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | Meta can't verify the webhook; you can't even register it |

The API Setup page also lets you add up to 5 **test recipient numbers** — add your own phone here. Until your app is verified by Meta, only those numbers can message the bot. For a controlled demo that limit is a feature, not an obstacle.

## Step 2 — App secrets

```bash
JWT_SECRET=                     # python -c "import secrets; print(secrets.token_urlsafe(48))"
PHONE_HASH_PEPPER=              # same generator
PHONE_ENCRYPTION_KEY=           # python -c "from app.core.encryption import generate_encryption_key as g; print(g())"
```

Without these, every message fails closed — deliberately. `GET /health` reports each as a boolean so you can confirm without exposing values.

## Step 3 — Fact-checking engine

```bash
ANTHROPIC_API_KEY=              # console.anthropic.com
SEARCH_API_KEY=                 # tavily.com
```

Without these, messages arrive and get a reply, but every verdict is honestly `insufficient_evidence` — there is no AI and no evidence. **These two are what make it actually work.**

## Step 4 — Enable image and voice-note support

Both are opt-in and off by default.

**Images / screenshots** — the most common real-world forward:

```bash
OCR_PROVIDER=claude_vision
```

That's the whole change. It reuses `ANTHROPIC_API_KEY`, needs no new dependency and no second service. Text is read from the image by the same Claude model already doing the fact-checking, then flows into the normal pipeline. Leave it empty and images honestly reply *"couldn't find readable text in this image."*

**Voice notes:**

```bash
SPEECH_TO_TEXT_PROVIDER=sarvam
SPEECH_TO_TEXT_API_KEY=         # sarvam.ai
```

Sarvam is purpose-built for Malayalam/Tamil/Hindi/English including code-mixing, which is what real WhatsApp voice notes in India actually sound like. Leave it empty and voice notes honestly reply *"couldn't reliably transcribe the audio."*

> Neither of these satisfies `decisions.md` §11's accuracy-benchmark rule. They make a real engine *available*; they are not a measured quality claim.

## Step 5 — Database and Redis

**Redis is genuinely required — not a nice-to-have.** Measured behaviour with Redis unreachable: `IdempotencyGuard.is_duplicate` raises `ConnectionError`, and it runs *before* the rate limiter in the webhook handler, so **every inbound message returns 500**. (The rate limiter fails open; idempotency does not. That asymmetry is defensible — Meta retries non-200 responses, so a 500 defers a message rather than paying for a duplicate fact-check — but it means "no Redis" equals "no bot", not "degraded bot".)

**With Docker:**

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis
```

**Without Docker** (Windows, no install): use a hosted Redis free tier — Redis Cloud gives 30MB free, which is far more than OTP keys and wamid dedupe need. Create a database, copy its connection string into `REDIS_URL`. Alternatives are Memurai (native Windows Redis) or WSL2 + `apt install redis`.

For the database, SQLite needs no service at all:

```bash
DATABASE_URL=sqlite+aiosqlite:///./sathyascan.db
```

That is also the *better-tested* option here — all 747 automated tests run on SQLite, and no migration in this project has ever run against real Postgres. Use Postgres for production, but do the first real-Postgres migration deliberately rather than during a demo.

Either way, create the schema:

```bash
cd backend && .venv/Scripts/python.exe -m alembic upgrade head
```

## Step 6 — Public HTTPS URL

Meta cannot deliver webhooks to `localhost`. Start the app:

```bash
cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Then in a second terminal, expose it. **ngrok** is the most widely documented (free account required):

```bash
ngrok http 8000
```

**Cloudflare Tunnel** is the lighter option — quick tunnels need no account at all:

```bash
cloudflared tunnel --url http://localhost:8000
```

Take the `https://<random>.ngrok-free.app` URL and in **Meta → WhatsApp → Configuration → Webhook**:

- **Callback URL**: `https://<your-url>/webhook/whatsapp`
- **Verify token**: exactly the `WHATSAPP_WEBHOOK_VERIFY_TOKEN` you set
- Click **Verify and save** — this triggers a `GET` your app must answer, which is why the token must match exactly
- Subscribe to the **`messages`** field (without this, nothing is delivered)

The free ngrok URL changes each restart; update Meta's config whenever it does.

## Step 7 — Confirm and test

```bash
cd backend && .venv/Scripts/python.exe -m scripts.golive_check
```

You want `Ready for: text, URL, image, audio`. Then message the bot from a registered test number:

| Send | Expect |
|---|---|
| A claim, e.g. *"Government announced free laptops for all students"* | "Analyzing…", then a verdict with sources |
| A link | A safety/phishing block plus a credibility assessment |
| A screenshot of a forwarded message | Text read from the image, then fact-checked |
| A voice note | Transcript, then fact-checked |

Two replies per message is correct: an immediate acknowledgement, then the result. The webhook returns `200` fast and analysis runs in the background by design.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Meta won't verify the webhook | `WHATSAPP_WEBHOOK_VERIFY_TOKEN` mismatch, or the app isn't running/reachable |
| Messages send but nothing arrives | The `messages` webhook field isn't subscribed |
| Every message 403s | `WHATSAPP_APP_SECRET` wrong or misnamed |
| Bot replies but always "insufficient evidence" | `ANTHROPIC_API_KEY` / `SEARCH_API_KEY` missing |
| Images say "no readable text" | `OCR_PROVIDER=claude_vision` not set |
| Voice notes say "couldn't transcribe" | Sarvam provider/key not set |
| Nothing at all, no logs | Number not in Meta's test-recipient list |
| Every message 500s, Meta shows delivery failures | Redis unreachable — see Step 5 |

Logs never contain phone numbers, OTP codes, or API keys — only truncated `phone_hash` values. That's deliberate, so don't expect to grep for a number when debugging.

## Related

- `docs/production-deployment.md` — real deployment beyond a demo tunnel
- `docs/feature-status-matrix.md` — exact status of every feature
- `docs/risks-and-open-questions.md` — the full limitation register
