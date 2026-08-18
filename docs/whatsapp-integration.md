# SathyaScan — WhatsApp Integration Architecture

Platform: **WhatsApp Business Cloud API (Meta)**, per PRD §33/§34.

**Locked decision (decisions.md §4)**: MVP is scoped to **synchronous, within-conversation-window replies only**. Every WhatsApp message SathyaScan sends is a direct response to a message the user just sent, inside the normal 24-hour service window. No proactive or scheduled message is sent by MVP code, under any feature. The "Check This Tomorrow" section below is retained as V2 design reference only — see the callout in that section for what's actually built now (nothing).

## Webhook lifecycle

1. **Verification handshake** (`GET /webhook/whatsapp`): compares `hub.verify_token` against a configured secret, echoes `hub.challenge` back. One-time setup step when registering the webhook URL with Meta.
2. **Signature verification** (`POST /webhook/whatsapp`): every request's `X-Hub-Signature-256` header is validated as an HMAC-SHA256 over the **raw request body**, using the Meta App Secret — computed and checked **before** any JSON parsing. Implementation note: a common bug is parse-then-verify, which reads the body twice and can silently break under certain ASGI middleware; use a raw-body-capturing dependency so the exact bytes Meta signed are the bytes verified.
3. **Message types handled**: `text`, `image`, `audio`, `video`, `document` (screenshots typically arrive as `image` — see screenshot-vs-photo heuristic below), and `interactive` (button/list replies, used for language-selection prompts). MVP does **not** implement a "check this tomorrow" interactive prompt — that flow doesn't exist until the feature itself is built in V2 (decisions.md §4/§14).
4. **Idempotency**: `analyses.source_wamid` is `UNIQUE`. On any Meta retry (undelivered-ack, timeout), the enqueue step checks for an existing row with that `wamid` and skips re-processing — preventing duplicate replies and duplicate downstream API/search costs.
5. **Enqueue and return 200**: the webhook's entire job is steps 2-4 plus pushing a Celery task. Nothing else runs synchronously.

## Media handling

The webhook payload only contains a `media_id`, never bytes. Flow (runs inside the async worker, not the webhook):
1. `GET /{media_id}` against the Graph API → returns a **temporary download URL (~5 minute TTL)**.
2. Fetch that URL immediately with the access token, before the TTL expires — this must happen promptly in the task chain, not deferred behind other queued work.
3. Persist to blob storage; record `whatsapp_media_id`, `sha256_hash` (dedupe + CSAM hash-matching input, see risks doc), `mime_type`, `file_size_bytes` in `media_attachments`. Uploads exceeding the configured `MAX_UPLOAD_SIZE_BYTES` (per media type) are rejected with a PRD §38-style friendly error before any storage or processing occurs.
4. **Before any AI analysis touches the file**, the safety gate (decisions.md §6, architecture.md §5) runs against the stored media. Only on a `passed` outcome does the file proceed to the Image/Video/Audio Analyzer stages. A `blocked` outcome short-circuits the whole pipeline — see `safety_gate_events` in database-schema.md.

**Screenshot vs. photo distinction** (PRD §12 treats screenshots as a distinct pipeline from general images): use lightweight heuristics — aspect ratio matching common phone screen ratios, absence of camera EXIF data, high OCR text-density in a first-pass scan — rather than an LLM judgment call, since WhatsApp doesn't natively distinguish these and the signal is cheap to compute deterministically.

## User-facing responsiveness

The AI pipeline can run 5-90+ seconds depending on content type. The very first async task in the chain sends an immediate WhatsApp acknowledgment ("🔍 Analyzing your content, this may take a moment...") **decoupled from the webhook's HTTP response** — this keeps the webhook fast (per architecture.md) while still giving the user immediate feedback that their message was received, independent of actual pipeline latency.

## Rate limits and abuse protection

Meta's Cloud API gates unique users you can message per rolling 24h (tiered, starting around 1K and scaling with phone number quality rating) plus a per-number throughput cap. On top of Meta's own limits (all locked in decisions.md §12, config-driven per §1):
- A **Redis token-bucket rate limiter per user** stops a single number from spamming/looping the pipeline.
- A **global concurrency cap on the `heavy` queue** (video/audio) bounds simultaneous GPU/third-party-API cost exposure independent of Meta's limits — this is a cost-control measure, not just an abuse measure.
- A **global concurrency cap on in-flight LLM/search calls**, separate from the `heavy` queue, since text-only analyses consume LLM/search budget without ever touching that queue.
- **Duplicate-content detection**: an identical or near-identical forward (matched via `media_attachments.sha256_hash` or text similarity) can reuse a recent analysis instead of reprocessing — see decisions.md §12 for the safety conditions (language match, Privacy Mode boundary respected).

## MVP messaging scope: within-window replies only (locked decision)

WhatsApp only permits free-form business-initiated messages within 24 hours of the user's last message to the business. PRD §21 ("Check This Tomorrow") describes a next-day scheduled re-check that sends a WhatsApp follow-up — in the overwhelming majority of cases this falls **outside** that 24-hour window, which would otherwise have required a pre-approved Meta message template before the feature could ship at all.

**Locked resolution (decisions.md §4)**: rather than build around this constraint for MVP, **the feature itself is cut from MVP scope**. SathyaScan sends messages only in direct response to a user message, always within the service window — no template messages, no proactive sends, no scheduling infrastructure invoked for messaging in MVP. This removes Meta template-approval lead time and per-template billing from the MVP critical path entirely.

**V2 precondition (unchanged by this decision, just deferred)**: if/when "Check This Tomorrow" is built, it still needs, before any code ships: (a) a submitted-and-approved Meta message template (e.g. `"Your fact-check on '{{claim}}' has an update: {{result}}. Reply to see details."`), (b) confirmation the utility-category template pricing is budgeted, (c) the data-handling constraints in decisions.md §14 (no raw media retained under Privacy Mode, claim-text-only snapshot, re-check evidence at execution time). See [risks-and-open-questions.md](risks-and-open-questions.md#whatsapp-messaging-costs) for the cost-budget gap that must close first.

## Session / conversation state

Keyed by `phone_number_hash`, stored in **Redis with a sliding TTL (~1 hour)** — not durable Postgres, since this is high-churn, ephemeral state:
- `last_analysis_id` — the user's most recent analysis, kept for general reference (e.g. "reopen my last result"); not currently used for scheduling anything, since "check this tomorrow" isn't built in MVP. Retained because it costs nothing to keep and is exactly what V2's scheduling flow would need later.
- `pending_prompt` — e.g. mid-flow language-selection state.
- `mid_conversation_language_override` — a same-session language choice that doesn't necessarily rewrite the user's saved `preferred_language`.

`conversation_sessions` in Postgres exists only as an optional durable audit mirror, not the hot read path.
