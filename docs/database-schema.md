# SathyaScan — Database Schema (PostgreSQL)

Expands the PRD's 5 illustrative entities (§35) into a normalized, production schema. All tables use `uuid` primary keys (`gen_random_uuid()`), `timestamptz` for all timestamps, and explicit FKs with a deliberately chosen `ON DELETE` behavior per the privacy requirements in PRD §22/§37.

This is a design reference for the Phase 0/2 SQLAlchemy models + Alembic migrations — not itself executable DDL.

**Locked decisions affecting this schema are recorded in [`decisions.md`](decisions.md).** In particular: all retention TTLs shown below are **configurable defaults**, not policy (decisions.md §8); `scheduled_checks` is a **V2 table, not migrated in MVP** (decisions.md §4/§14); `phone_number_*` handling rules in decisions.md §7 apply to every column below that touches identity.

**Phase 2 implementation status**: `users`, `analyses`, `claims`, `evidence`, `source_credibility_registry`, and `usage_ledger` are implemented (`backend/app/models/`, migration `0001_phase2_initial_schema.py`) — this is the exact subset the text fact-checking pipeline needs. One column was added beyond this document's original design: **`analyses.content_fingerprint`** (`varchar(64)`, indexed — SHA-256 hex of the normalized claim text) — needed for duplicate-content detection (decisions.md §12), not previously specified here. `claims` also gained `investigation_complete` / `incomplete_reason` (already documented below, now implemented exactly as specified) and `policy_override_applied` (`boolean` — true when the deterministic evidence-tier guard overrode the LLM's suggested classification; kept for audit/appeals review, see `app/agent/classification.py`).

**Phase 3 implementation status**: `media_attachments`, `safety_gate_events`, `media_forensics_results`, `benchmark_datasets`, `benchmark_runs` are now also implemented (migration `0002_phase3_media_safety.py`) — see each section below for Phase-3-specific notes. `analyses.status` gains a new valid application-level value, **`blocked`** (safety-gate short-circuit), alongside the existing `pending | processing | completed | failed` — no migration needed since the column is a plain `varchar(20)` with no DB-level CHECK constraint. `analyses.content_fingerprint` for `input_type='image'` is the SHA-256 of the downloaded image bytes (reusing the same column, not a schema change) rather than of claim text.

**Phase 4 implementation status**: `url_safety_scans` is now implemented (migration `0003_phase4_url_safety.py`), unchanged from the design below. `analyses.input_type='url'` is now genuinely used (it was already a documented, valid value, just unused until this phase) — `analyses.content_fingerprint` for `input_type='url'` is the SHA-256 of the URL string itself, and `analyses.input_text` stores **the URL, never the fetched page's content** — deliberate, per decisions.md §3's "no full-text storage of copyrighted articles" principle, extended here to the analysis's own input, not just retrieved evidence (mirrors how Phase 3 never persists raw media bytes). Fetched page content lives only in memory for the duration of one pipeline run, feeding claim extraction, and is never written to any column in full. `url_safety_scans.reasons` stores structured finding codes + params (`app/agent/tools/url_safety.py`'s `FindingCode`), not pre-rendered text, so the same row could in principle be re-localized later; `recommendation` is rendered once, in the analysis's language, at persist time.

**Phase 5 implementation status** (audio §5a, video §5b — reprioritized ahead of Dashboard/Hardening at the user's explicit instruction, see docs/phased-plan.md): `transcripts` is now implemented (migration `0004_phase5_audio.py`), unchanged from the design below and shared, unmodified, by both audio-clip transcripts and video-extracted-audio transcripts — no modality-specific columns needed, since `media_attachment_id` already disambiguates via `media_attachments.media_type`. **`video_frames` is a NEW table** (migration `0005_phase5_video.py`), not in this document's original design set (which predates any video work) — see its own section below. `analyses.input_type` gains real usage of its already-documented `audio`/`video` values; `analyses.input_text` stays empty for both (the real content lives in `transcripts`/`video_frames`, not duplicated into `analyses`, mirroring Phase 3's OCR-text-never-in-`analyses.input_text` precedent). `media_attachments.media_type='audio'|'video'` and `media_forensics_results.tool_name='audio_voice_detect'|'video_deepfake'` all use already-documented values, genuinely populated for the first time — audio/video forensics results are only ever written when a real analysis actually ran (`NullAudioForensicsProvider`/`NullVideoForensicsProvider` report `PROVIDER_UNAVAILABLE`, which writes NO row, same "absence of a row is the signal" precedent Phase 3 set for `media_forensics_results`). No frame images or raw audio/video bytes are ever persisted — same no-blob-storage precedent as Phase 3.

**Phase 6 implementation status**: `dashboard_accounts`, `otp_verifications`, and `audit_log` are now implemented (migration `0006_phase6_dashboard.py`), matching the design below with one deliberate deviation: `dashboard_accounts.email` is `varchar(255)` here, not `citext` — Phase 6 is WhatsApp-OTP-only (`email`/`password_hash` stay nullable and unused, confirmed scope decision, see `api-design.md`), so case-insensitive email lookup was never exercised; revisit if/when password-based dashboard login is ever built. `audit_log.metadata` is written by `app/agent/audit.py`, which defensively rejects (raises, not silently strips) any attempt to include an OTP code, JWT, password, or phone number/hash — verified by test. `media_attachments.retention_expires_at` (already documented below, previously unpopulated) is now set at creation time by Image/Audio/VideoPipeline from `compute_media_retention_expiry` (`app/agent/retention.py`) and actually scanned by a real periodic purge job — see the Privacy Mode section at the end of this document for its now-verified status. `analyses` gained no new columns; its existing `privacy_mode_snapshot`/`is_deleted`/`created_at` are what the purge's analysis-level pass reads.

Still design-reference-only, not migrated: `conversation_sessions`, `reports`, `scheduled_checks`, `explore_*`, `rate_limit_usage`, `data_deletion_requests`, `appeals`, `admin_users` — none of these were required by Phase 6's confirmed scope (Explore/PDF Reports deferred to their already-locked V2 slot; Admin/Appeals/`/overview`/scheduled-checks/`conversation_sessions`/full account deletion explicitly excluded per the user's Phase 6 instructions).

**Phase 7 implementation status**: no schema changes — Phase 7 was security/production hardening, not new data. Note on `rate_limit_usage` above: dashboard API rate limiting IS now real (`app/api/v1/deps.py`/`app/api/v1/routers/auth.py`), but is Redis-backed (fixed-window counters, reusing Phase 1's `app/core/rate_limit.py::RateLimiter` unchanged), never touching a `rate_limit_usage` table — same "Redis is the hot path, Postgres is an optional durable mirror if ever needed" pattern `conversation_sessions` already documents above. Migration cycle re-verified clean (upgrade→downgrade→upgrade, still head `0006`).

## Identity

```sql
users (
  id uuid PK,
  phone_number_encrypted bytea NOT NULL,     -- AES-256-GCM via KMS envelope encryption; needed to send replies
                                              -- NEVER returned by any dashboard API response (decisions.md §7);
                                              -- NEVER written to logs — reference user_id or phone_number_hash instead
  phone_number_hash bytea NOT NULL UNIQUE,   -- keyed HMAC-SHA256(phone, PHONE_HASH_PEPPER) — NOT bare SHA-256
                                              -- (phone number space is small/guessable; pepper prevents rainbow-table correlation)
                                              -- pepper lives in secrets manager, never in DB or source (decisions.md §7)
  preferred_language varchar(5) NOT NULL DEFAULT 'en',  -- ml | en | hi | ta
  auto_detect_language boolean DEFAULT true,
  privacy_mode boolean NOT NULL DEFAULT true,  -- [Decision] default ON — confirmed with user
  is_deleted boolean DEFAULT false,
  deleted_at timestamptz,
  created_at, updated_at, last_active_at timestamptz
)
-- indices: UNIQUE(phone_number_hash), btree(last_active_at)

dashboard_accounts (
  id uuid PK,
  user_id uuid FK users(id) UNIQUE NOT NULL,
  email citext UNIQUE,
  password_hash text,               -- nullable: MVP is WhatsApp-OTP-only, see api-design.md
  email_verified boolean DEFAULT false,
  created_at, updated_at
)

otp_verifications (
  id uuid PK,
  user_id uuid FK users(id),
  channel varchar(20) NOT NULL,      -- 'whatsapp' | 'email'
  purpose varchar(30) NOT NULL,      -- 'dashboard_link' | 'login'
  code_hash text NOT NULL,
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  attempts smallint DEFAULT 0,
  created_at timestamptz
)
-- index: btree(user_id, purpose, expires_at)
```

## Conversation state

```sql
-- Redis is the hot-path store (TTL-based, keyed by phone_number_hash).
-- This table is an optional durable audit mirror, not the primary read path.
conversation_sessions (
  id uuid PK,
  user_id uuid FK users(id),
  context jsonb NOT NULL DEFAULT '{}',  -- last_analysis_id, pending_prompt, language_override_mid_flow
  expires_at timestamptz,
  updated_at timestamptz
)
```

## Core analysis

```sql
analyses (
  id uuid PK,
  user_id uuid FK users(id) ON DELETE CASCADE,
  session_id uuid FK conversation_sessions(id) NULL,
  input_type varchar(20) NOT NULL,     -- text | image | video | audio | url | screenshot
  input_text text,                     -- raw text or URL
  content_fingerprint varchar(64) NOT NULL,  -- SHA-256 hex of the normalized claim text —
                                              -- duplicate-content detection key (decisions.md §12).
                                              -- ADDED IN PHASE 2, not in the original schema design.
  source_wamid varchar(128) UNIQUE,    -- WhatsApp message ID — idempotency key
  status varchar(20) NOT NULL DEFAULT 'pending',  -- pending | processing | completed | failed
  overall_result varchar(30),          -- verified | false | misleading | partially_true |
                                        -- unverified | opinion | satire | insufficient_evidence
  language varchar(5) NOT NULL,
  privacy_mode_snapshot boolean NOT NULL,  -- captured at analysis time, immutable even if user later flips the setting
  error_code varchar(50),
  processing_duration_ms integer,
  evidence_search_count smallint DEFAULT 0,   -- counted against MAX_EVIDENCE_SEARCHES_PER_ANALYSIS (decisions.md §1)
  llm_tool_call_count smallint DEFAULT 0,     -- counted against MAX_LLM_TOOL_CALLS_PER_ANALYSIS (decisions.md §1)
  budget_limit_hit boolean DEFAULT false,     -- true if a per-analysis cap was hit at any point during processing.
                                               -- NOTE: this does NOT mean every claim in the analysis became
                                               -- insufficient_evidence — only claims still unresolved at the
                                               -- moment the limit tripped are forced to insufficient_evidence
                                               -- (decisions.md §1A); claims already fully investigated beforehand
                                               -- keep their real result (verified/false/misleading/unverified/etc).
                                               -- This is an analysis-level flag; see claims.incomplete_reason below
                                               -- for the per-claim, more specific record.
  reused_from_analysis_id uuid FK analyses(id) NULL,  -- set when served from duplicate-content detection (decisions.md §12)
  is_deleted boolean DEFAULT false,    -- UI-hide flag; background hard-purge after grace window (see below)
  deleted_at timestamptz,
  created_at, completed_at timestamptz
)
-- indices:
--   btree(user_id, created_at DESC) WHERE is_deleted=false   [history queries]
--   UNIQUE(source_wamid)                                     [idempotency]
--   btree(status) WHERE status IN ('pending','processing')   [worker recovery / stuck-job detection]
--   btree(content_fingerprint)                                [duplicate-content lookup, decisions.md §12 —
--                                                                Phase 2 scopes reuse to same-user-only, see
--                                                                app/agent/duplicate_detection.py]
-- NOTE: processing_duration_ms is also checked in-flight against MAX_PROCESSING_DURATION_SECONDS
-- (decisions.md §12/§15) — a worker-level wall-clock timeout, not just a post-hoc metric.

claims (
  id uuid PK,
  analysis_id uuid FK analyses(id) ON DELETE CASCADE,
  claim_text text NOT NULL,
  claim_order smallint NOT NULL,
  result varchar(30) NOT NULL,         -- verified | false | misleading | partially_true |
                                        -- unverified | opinion | satire | insufficient_evidence
                                        --
                                        -- unverified vs insufficient_evidence are DELIBERATELY DISTINCT,
                                        -- not interchangeable (decisions.md §1A):
                                        --   unverified            = investigation completed, evidence too
                                        --                            weak/inconsistent to classify
                                        --   insufficient_evidence = investigation could NOT be completed
                                        --                            (budget/search/tool-call/provider/
                                        --                            timeout/infra limit) — see incomplete_reason
                                        -- Both are first-class, non-definitive outcomes, never errors.
                                        -- A budget/tool-call limit is NEVER read as evidence toward
                                        -- verified/false — hitting one always yields insufficient_evidence,
                                        -- never a lower-confidence verified/false/misleading.
  investigation_complete boolean NOT NULL DEFAULT true,
                                        -- false if and only if result = 'insufficient_evidence' (decisions.md §1A)
  incomplete_reason varchar(30),       -- NULL unless investigation_complete = false, then one of:
                                        -- search_limit | tool_call_limit | provider_unavailable |
                                        -- timeout | infra_error | other
  claim_confidence numeric(4,3),       -- 0.000-1.000 — separate scores per PRD §25
  media_confidence numeric(4,3),
  evidence_strength numeric(4,3),
  reasoning_text text,                 -- hedged natural-language explanation (PRD §39)
  category varchar(30),                -- gov_scheme | elections | health | tech | ai_media | scam | other
                                        -- (feeds Explore's "Frequently Checked Topics"; elections/gov_scheme
                                        -- are the current "high-impact" categories requiring a raised evidence
                                        -- bar for False/Verified per decisions.md §3/§5 — finalized in Phase 2
                                        -- as the config-driven HIGH_IMPACT_CATEGORIES set, default "elections,gov_scheme")
  evidence_tier_met smallint,          -- 1/2/3 — the strongest evidence tier actually backing this claim's
                                        -- result (decisions.md §3); used to enforce "strong evidence required
                                        -- for a strong False/Verified" without re-deriving it from evidence[] each time
  policy_override_applied boolean NOT NULL DEFAULT false,  -- ADDED IN PHASE 2: true when the deterministic
                                        -- evidence-tier guard overrode the LLM's suggested classification
                                        -- (app/agent/classification.py) — kept for audit/appeals review
  created_at timestamptz
)
-- indices: btree(analysis_id, claim_order); GIN/pgvector embedding for Explore clustering (see below);
--          btree(result, incomplete_reason) WHERE result='insufficient_evidence'  [internal analytics —
--          e.g. "what fraction of insufficient_evidence is search_limit vs provider_unavailable", used to
--          tell whether MAX_EVIDENCE_SEARCHES_PER_ANALYSIS etc. are tuned too tight, decisions.md §1A]
-- CHECK (invariant, enforce in application layer and/or a DB constraint):
--   investigation_complete = false  <=>  result = 'insufficient_evidence'
--   result = 'unverified'  =>  investigation_complete = true AND incomplete_reason IS NULL

evidence (
  id uuid PK,
  claim_id uuid FK claims(id) ON DELETE CASCADE,
  source_url text,
  source_domain varchar(255),
  source_title text,
  publisher_name varchar(255),
  stance varchar(20) NOT NULL,          -- supporting | contradicting | no_reliable_evidence
  relevance_score numeric(4,3),         -- search relevance/rank ONLY — never used as a credibility signal
                                         -- (decisions.md §3: "search-engine ranking is not evidence quality")
  credibility_score numeric(4,3),       -- derived ONLY from source_credibility_registry.credibility_tier below,
                                         -- never from relevance_score/search rank
  snippet_text text,                    -- short snippet only — not full-text scraping (copyright risk, see risks doc)
  published_at timestamptz,
  retrieved_at timestamptz NOT NULL
)
-- indices: btree(claim_id), btree(source_domain)

source_credibility_registry (
  id uuid PK,
  domain varchar(255) UNIQUE NOT NULL,
  credibility_tier varchar(30) NOT NULL,  -- tier_1_gov_official | tier_1_primary_doc | tier_1_factcheck_org |
                                           -- tier_2_reputable_news | tier_2_research | tier_3_other |
                                           -- low_trust | blacklisted  (three-tier system, decisions.md §3)
  region varchar(10),                      -- e.g. 'IN' for regional fact-checkers
  notes text,
  updated_at timestamptz
)
-- Seed list should include known Indian fact-checkers: BOOM, Vishvas News, Alt News,
-- Factly, PIB Fact Check — see risks-and-open-questions.md (multilingual evidence quality)
-- This registry is the ONLY source of credibility_score above — a domain absent from
-- this table defaults to tier_3_other, never inferred from search rank.
```

## Media

**Phase 3 implementation note**: `media_attachments` is implemented with a trimmed field set — `retention_expires_at`/`deleted_at`-driven purge jobs are Phase 5 scope (Privacy Mode retention), not built yet, so those columns exist but aren't actively managed. More significantly, **`storage_path` stays permanently NULL in Phase 3** — no blob storage is implemented at all; media is downloaded to memory, validated, safety-checked, and OCR'd within a single background task, then discarded. "Secure cleanup" holds by construction (nothing durable to clean up), not via a purge job. `media_forensics_results` is implemented and migrated but stays **empty** — no real image-forensics provider is wired this phase (`NullImageForensicsProvider` always reports unavailable), and rather than write placeholder values into the `NOT NULL provider_name`/`model_version` columns for a check that didn't really happen, the absence of a row is the signal that no forensics was attempted.

```sql
media_attachments (
  id uuid PK,
  analysis_id uuid FK analyses(id) ON DELETE CASCADE,
  whatsapp_media_id varchar(128),
  storage_path text,                  -- ALWAYS NULL in Phase 3 — no blob storage implemented, see note above
  media_type varchar(20) NOT NULL,    -- image | video | audio | document
  mime_type varchar(100),             -- the ACTUAL sniffed type (magic bytes), never the claimed one
  file_size_bytes bigint,
  sha256_hash bytea,                  -- dedupe + input to CSAM hash-matching safety gate
  retention_expires_at timestamptz,   -- short TTL under privacy mode vs normal retention — NOT YET ACTIVELY managed (Phase 5)
  deleted_at timestamptz
)
-- index: btree(retention_expires_at) WHERE deleted_at IS NULL   [purge job scan]

media_forensics_results (
  id uuid PK,
  media_attachment_id uuid FK media_attachments(id) ON DELETE CASCADE,
  tool_name varchar(50) NOT NULL,      -- image_analyzer | video_deepfake | audio_voice_detect
  provider_name varchar(50) NOT NULL,  -- e.g. hive | sightengine | reality_defender — MANDATORY, user-facing
                                        -- (decisions.md §2: provider + version must be shown, not just logged)
  model_version varchar(50) NOT NULL,  -- third-party API/model version — MANDATORY, user-facing
  ai_generated_probability numeric(4,3),
  manipulation_score numeric(4,3),
  deepfake_score numeric(4,3),
  raw_output jsonb,
  created_at timestamptz
)
-- Architected for multi-provider: a single media_attachment can have >1 row here
-- (one per provider) once a second detector is added for ensemble checking
-- (decisions.md §2 — "architecture supports a second provider"). Stays EMPTY
-- in Phase 3 — no real provider wired, see note above.

-- Phase 5 implementation note: migrated exactly as designed
-- (migration 0004_phase5_audio.py). `engine` stays NULL in this build —
-- NullSpeechToTextProvider is the only wired provider (decisions.md §11's
-- benchmark-before-lock-in rule, same precedent as OCR in Phase 3), so
-- `text`/`confidence` are only ever populated by a real STT run once one
-- exists. Shared unmodified by audio-clip transcripts (AudioPipeline) and
-- video-extracted-audio transcripts (VideoPipeline) — media_attachment_id
-- already disambiguates via media_attachments.media_type.
transcripts (
  id uuid PK,
  media_attachment_id uuid FK media_attachments(id) ON DELETE CASCADE,
  language varchar(5),
  text text,
  confidence numeric(4,3),
  engine varchar(50),                  -- whisper | indicwhisper | ... (post-benchmark)
  created_at timestamptz
)
-- index: btree(media_attachment_id)

-- Phase 5b: NEW table, not in this document's original design set (which
-- predates any video work) — the closest existing precedent is
-- `transcripts` above, mirrored here for OCR-per-frame results instead of
-- a whole-clip transcript. Deliberately NO frame image is ever stored —
-- only the extracted TEXT, timestamp, language, and confidence, consistent
-- with Phase 3's no-blob-storage precedent. Migrated in
-- 0005_phase5_video.py. Populated via the EXACT SAME OCRTool Phase 3 built
-- (app/agent/tools/ocr.py) — no second OCR engine.
video_frames (
  id uuid PK,
  media_attachment_id uuid FK media_attachments(id) ON DELETE CASCADE,
  frame_timestamp_ms integer NOT NULL,
  extracted_text text,
  detected_language varchar(5),
  ocr_confidence numeric(4,3),
  created_at timestamptz
)
-- index: btree(media_attachment_id)
```

## Safety gate (prohibited-content check — decisions.md §6)

**Phase 3 implementation note**: implemented and migrated exactly as designed below. The gate itself runs as a non-bypassable stage before any media analysis (`app/agent/safety_gate.py`), but only `NullSafetyProvider` is wired — every row this table records in Phase 3 will show `outcome='passed'`, `provider_name='null_passthrough_not_a_real_check'`. This is not a real content-safety check; see decisions.md §6 — public exposure remains blocked pending the mandatory legal/professional review.

```sql
-- Deliberately minimal. This table records that a check happened and what the
-- outcome was — it must NEVER store the flagged content itself, a description
-- of it, or anything beyond what's needed for the (pending, legally-reviewed)
-- reporting process to act on. Vendor/procedure are NOT decided by this schema;
-- this is the audit slot the pipeline writes to, whatever the final vendor is.
safety_gate_events (
  id uuid PK,
  media_attachment_id uuid FK media_attachments(id) ON DELETE CASCADE NULL,
  analysis_id uuid FK analyses(id) ON DELETE CASCADE,
  check_type varchar(30) NOT NULL,      -- csam_hash_match | content_classifier | other
  outcome varchar(20) NOT NULL,         -- passed | blocked | error
  provider_name varchar(50),            -- hash-matching / classifier vendor, once approved
  reference_id text,                    -- vendor's own case/match reference, if any — NOT the content
  reported_at timestamptz,              -- when/if escalated through the mandated reporting channel
  created_at timestamptz
)
-- index: btree(analysis_id), btree(outcome) WHERE outcome='blocked'
-- Access to this table should be more restricted than any other (audit_log-level
-- or higher), since even metadata about a block is sensitive.
```

## Benchmarking framework (decisions.md §2, §11)

**Phase 3 implementation note**: tables and a minimal recording helper (`app/agent/benchmarking.py`) are implemented and migrated, but **no real benchmark has been run** — this build environment has no OCR provider credentials and no curated Malayalam/Tamil/Hindi/English benchmark image set. decisions.md §11's sequencing rule ("benchmarking happens before final engine selection") is therefore explicitly **not yet satisfied**; this is a documented, flagged gap (see docs/risks-and-open-questions.md), not a silent pass. Both tables stay empty until a real benchmark run exists.

```sql
-- Shared infrastructure for evaluating media-forensics detectors AND OCR/ASR
-- engines against real WhatsApp-quality Malayalam/Tamil/Hindi/English content
-- BEFORE any engine is locked for production (Phase 3 for OCR, pre-V2 for ASR,
-- ongoing for media forensics).
benchmark_datasets (
  id uuid PK,
  name varchar(100) NOT NULL,           -- e.g. "malayalam-ocr-whatsapp-screenshots-v1"
  modality varchar(20) NOT NULL,        -- ocr | asr | image_forensics | video_forensics | audio_forensics
  language varchar(5),                  -- ml | ta | hi | en | null (forensics may be language-agnostic)
  sample_count integer,
  source_description text,              -- how samples were collected (must respect consent/licensing)
  created_at timestamptz
)

benchmark_runs (
  id uuid PK,
  dataset_id uuid FK benchmark_datasets(id),
  tool_name varchar(50) NOT NULL,       -- which engine/provider under test
  model_version varchar(50),
  run_at timestamptz,
  accuracy_metrics jsonb,               -- precision/recall/F1/WER/CER as applicable to modality
  notes text
)
-- Query benchmark_runs ordered by dataset_id + accuracy_metrics to compare
-- candidate engines before Phase 3's OCR lock-in and pre-V2's ASR lock-in.
```

## URL safety (kept distinct from `evidence`, per PRD §11 vs §10)

**Phase 4 implementation note**: implemented and migrated exactly as designed below (migration `0003_phase4_url_safety.py`). Written for every URL analysis — including when the fetch itself never happened (SSRF-blocked or robots.txt-disallowed still produce a real row; `risk_level='critical'` with a `private_address_blocked` reason is itself a genuine, useful safety result, not a placeholder). `reasons` holds `[{"code": "...", "params": {...}}, ...]` — structured `FindingCode` values from `app/agent/tools/url_safety.py`, not pre-rendered strings. `threat_intel_matches` stays `NULL` in this build (`NullThreatIntelProvider` — no Safe Browsing-style provider wired, explicitly OPTIONAL per phased-plan.md's Phase 4 scope, decisions.md-style flagged gap same as OCR/image-forensics). `risk_level`/`recommendation`/`reasons` are all real, computed by genuine deterministic heuristics — domain-pattern, HTTPS, redirect-chain, phishing-keyword, fake-login-form checks, plus WHOIS-based domain age (`app/web/domain_age.py`) — none of which need credentials.

```sql
url_safety_scans (
  id uuid PK,
  analysis_id uuid FK analyses(id) ON DELETE CASCADE,
  url text NOT NULL,
  risk_level varchar(20) NOT NULL,     -- safe | low | medium | high | critical
  reasons jsonb NOT NULL DEFAULT '[]',
  recommendation text,
  threat_intel_matches jsonb,
  scanned_at timestamptz
)
```

## Reports & scheduling

**PDF reports have no dedicated table.** Phase 8 built `GET
/api/v1/history/{id}/report.pdf` (`app/agent/pdf_report.py`) to generate the
PDF **on demand** and stream it directly in the HTTP response — never
written to disk or any blob store, so the `reports`/`pdf_storage_path`
design sketched in an earlier draft of this document was never built and is
removed here rather than left as a misleading "planned" table. A
`report_downloaded` `audit_log` entry is written on every download instead.

```sql
-- ============================================================================
-- scheduled_checks — Phase 8 "Check This Tomorrow" BACKEND (migration 0007).
-- Built per the user's explicit "PHASE 8 EXECUTION DECISIONS" (2026-08-16):
-- the full scheduling/re-check/diff/notification-generation pipeline is real
-- and tested. The one thing NOT done is the actual proactive WhatsApp send —
-- still blocked on decisions.md §4's three preconditions (Meta requirements,
-- template approval, cost budget). See app/models/scheduled_check.py and
-- app/agent/scheduled_check_runner.py for the full rationale.
-- ============================================================================
scheduled_checks (
  id uuid PK,
  user_id uuid FK users(id) ON DELETE CASCADE,
  source_analysis_id uuid FK analyses(id) ON DELETE SET NULL,  -- NOT CASCADE, deliberate: see below
  claim_text_snapshot text NOT NULL,    -- minimum data needed to re-verify; NOT the raw media (decisions.md §14)
  category varchar(30),
  language varchar(5) NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'pending',  -- pending | running | completed | failed | cancelled
  scheduled_for timestamptz NOT NULL,
  executed_at timestamptz,
  previous_result_snapshot jsonb,
  new_result_snapshot jsonb,
  credibility_changed boolean,
  attempts smallint NOT NULL DEFAULT 0,
  max_attempts smallint NOT NULL DEFAULT 3,
  error_message text,
  notification_status varchar(20) NOT NULL DEFAULT 'not_sent',  -- not_sent | generated | blocked_by_policy
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
)
-- indices: btree(user_id), btree(source_analysis_id), btree(status), btree(scheduled_for)
--
-- source_analysis_id is SET NULL, not CASCADE: the whole point of this table
-- is that a scheduled check can run LATER using only its OWN snapshot,
-- independent of whether the source analysis still exists. Privacy Mode's
-- short retention window (media in 15min, analyses in ~48h) means a
-- Privacy-Mode-ON user's source analysis will very often be purged before
-- "tomorrow" arrives — CASCADE would silently delete the scheduled check
-- too. app/agent/retention.py explicitly nulls this column (child-to-parent
-- SQL, not relying on the DB's FK action) before deleting an analysis.
--
-- Execution: an in-process asyncio periodic task
-- (app/agent/scheduled_check_runner.py, same non-Celery pattern as the
-- retention purge and the Explore rollup below) picks up due, pending rows
-- and re-runs the SAME app.agent.claim_pipeline_shared.investigate_claim
-- used by a fresh analysis — no second fact-checking system. Provider/
-- timeout failures are already absorbed into an "insufficient_evidence"
-- verdict by investigate_claim itself; the runner's own retry/max_attempts
-- logic only fires for genuinely unexpected crashes.
```

## Explore (privacy-safe aggregation)

```sql
-- CRITICAL INVARIANT: this table has NO foreign key to users/analyses/claims
-- anywhere — not "convention", literally no such column exists to join on.
-- Populated by a periodic asyncio rollup (app/agent/explore_rollup.py,
-- NOT Celery — matches the retention-purge/scheduled-check-runner pattern),
-- which reads privacy-mode-excluded, completed analyses and re-clusters them
-- by cluster_fingerprint each tick (idempotent recompute, not an increment).
explore_claim_clusters (
  id uuid PK,
  cluster_fingerprint varchar(64) UNIQUE NOT NULL,  -- app.agent.duplicate_detection.compute_fingerprint
                                                     -- (SHA-256 of normalized claim text) — reused, not new
  representative_claim_text text NOT NULL,
  category varchar(30),
  language varchar(5) NOT NULL,
  credibility_status varchar(30) NOT NULL,  -- same ResultLabel vocabulary as claims.result (decisions.md §1A);
                                             -- the MOST RECENT contributing claim's result, never blended
  credibility_score numeric(4,3),           -- average claim_confidence across contributing claims this rollup
  source_count smallint NOT NULL DEFAULT 0,
  check_count integer NOT NULL DEFAULT 0,   -- how many times a matching claim has been checked; "trending" derives
                                             -- from this + last_seen_at
  first_seen_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
)
-- indices: btree(category), btree(credibility_status), btree(last_seen_at)
--
-- The pgvector-embedding-based fuzzy clustering originally sketched here was
-- NOT built — exact-fingerprint clustering (same normalization as duplicate
-- detection) was judged the smallest correct solution for Phase 8's scope;
-- near-duplicate phrasing variants of the same claim are NOT automatically
-- merged into one cluster. explore_daily_rollups (a separate day-bucketed
-- table) was also not built — check_count/last_seen_at on the cluster row
-- itself already serve the "trending" sort without a second table.
```

## Notifications & analysis sessions (Phase 8)

```sql
-- notifications — channel-abstract by design (channel column), so the SAME
-- table/shape covers today's in_app delivery and tomorrow's real WhatsApp
-- delivery once decisions.md §4's preconditions are met — no redesign
-- needed at activation time. channel='whatsapp' rows ARE created and
-- queryable today; only the actual Meta API call is withheld — every such
-- row is written with delivery_status='blocked_by_policy'
-- (app/agent/notifications.py never invokes the WhatsApp sender).
notifications (
  id uuid PK,
  user_id uuid FK users(id) ON DELETE CASCADE,
  notification_type varchar(40) NOT NULL,  -- scheduled_check_completed | credibility_changed |
                                            -- scheduled_check_failed | security_event
  channel varchar(20) NOT NULL DEFAULT 'in_app',   -- in_app | whatsapp
  title varchar(200) NOT NULL,
  body text NOT NULL,                      -- short, already rendered in the user's language at write time —
                                            -- never raw claim/evidence content (same "structural facts only"
                                            -- precedent as audit_log)
  related_entity_type varchar(50),         -- 'analysis' | 'scheduled_check' | NULL
  related_entity_id uuid,                  -- no FK — full detail is fetched via the EXISTING History API,
                                            -- not duplicated here
  delivery_status varchar(20) NOT NULL DEFAULT 'pending',  -- pending | delivered | blocked_by_policy | failed
  read_at timestamptz,
  created_at timestamptz NOT NULL
)
-- indices: btree(user_id), btree(created_at)

-- analysis_sessions — Phase 8 "Conversation Sessions": user-organized
-- grouping of History items (open a session, add checks to it). NOT the
-- same table as the `conversation_sessions` design above (that one is
-- short-lived WhatsApp mid-flow state, Redis-hot-path) — deliberately named
-- differently to avoid colliding with that already-reserved design; both
-- can coexist.
analysis_sessions (
  id uuid PK,
  user_id uuid FK users(id) ON DELETE CASCADE,
  name varchar(200) NOT NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
)
-- analyses.session_id (added via migration 0007, batch_alter_table — plain
-- add_column+create_foreign_key raises NotImplementedError on SQLite) is a
-- single nullable FK ON DELETE SET NULL: one analysis belongs to at most one
-- session at a time (not a many-to-many tagging system). Deleting a session
-- explicitly nulls out every member analysis's session_id before deleting
-- the session row itself (child-to-parent, not relying on the FK action).
```

## Ops / compliance

```sql
rate_limit_usage (id uuid PK, user_id uuid, window_start timestamptz, window_type varchar(20), message_count int)
usage_ledger (id uuid PK, date date NOT NULL UNIQUE, evidence_search_count bigint DEFAULT 0, llm_tool_call_count bigint DEFAULT 0, estimated_cost_usd numeric(10,2) DEFAULT 0, circuit_breaker_tripped boolean DEFAULT false, tripped_at timestamptz)
  -- backs the daily spend circuit breaker (decisions.md §1); incremented per-analysis,
  -- checked against DAILY_SPEND_CIRCUIT_BREAKER_USD (config, not hard-coded)
audit_log (id uuid PK, actor_type varchar(20), action varchar(100), entity_type varchar(50), entity_id uuid, metadata jsonb, created_at timestamptz)
  -- deliberately no FK from entity_id to users — audit records must survive
  -- the referenced user being anonymized/deleted (see account deletion, below)
-- data_deletion_requests — NOT BUILT. Phase 8 implemented full account
-- deletion as a SYNCHRONOUS API call (DELETE /api/v1/account) instead of a
-- queued table — see the "Full account deletion" paragraph below. Removed
-- from this list rather than left as a misleading "planned" table.
```

## Admin, Appeals & Moderation (Phase 9 — roadmap §9.1/§9.2/§9.3)

```sql
-- admin_users — a SEPARATE identity table from `users` (no FK relationship
-- to it at all). Admins are internal operators (email+password login, NOT
-- WhatsApp-OTP) — a genuinely different identity domain from end users,
-- kept structurally distinct rather than a role flag on `users`. No public
-- signup endpoint exists anywhere; rows are created only via
-- scripts/create_admin.py, run locally with direct DB access.
admin_users (
  id uuid PK,
  email varchar(255) UNIQUE NOT NULL,     -- stored lowercased; no citext dependency
  password_hash varchar(255) NOT NULL,    -- real bcrypt hash — deliberately NOT the keyed-HMAC
                                           -- pattern hash_phone_number/hash_otp_code use elsewhere
                                           -- (see app/core/admin_security.py's docstring)
  role varchar(20) NOT NULL DEFAULT 'admin',  -- 'admin' | 'moderator'
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  last_login_at timestamptz
)

-- appeals — matches the original architecture sketch, plus a resolved_by
-- admin reference (accountability) not in that original sketch. Users can
-- NEVER modify a fact-check result through this table — it only ever
-- records a request for human review; approving an appeal does not
-- automatically rewrite the original analysis/claim row (see
-- app/models/appeal.py's docstring for why that's a deliberate boundary,
-- not an oversight).
appeals (
  id uuid PK,
  user_id uuid FK users(id) ON DELETE CASCADE,
  analysis_id uuid FK analyses(id) ON DELETE CASCADE,   -- CASCADE: an appeal has no purpose
                                                          -- once its subject analysis is gone
  claim_id uuid FK claims(id) ON DELETE SET NULL,
  reason_text text NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'open',
    -- open | under_review | approved | rejected | escalated | cancelled
  admin_notes text,
  resolved_by uuid FK admin_users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  resolved_at timestamptz
)

-- moderation_reports — NOT in the original architecture sketch (new this
-- phase). A human-review ticketing system, NOT a new AI moderation system
-- — reuses the EXISTING safety-gate/URL-safety detection infrastructure;
-- this table is the downstream "a human flagged this" record those don't
-- cover. Reporting deliberately does NOT require owning the target (the
-- whole point is flagging someone ELSE's content or a public Explore item).
moderation_reports (
  id uuid PK,
  reporter_user_id uuid FK users(id) ON DELETE CASCADE,
  report_type varchar(30) NOT NULL,      -- spam | abuse | suspicious_content | malicious_url | other
  target_type varchar(20) NOT NULL,      -- analysis | url | explore_claim | other
  target_analysis_id uuid FK analyses(id) ON DELETE SET NULL,
  target_explore_cluster_id uuid FK explore_claim_clusters(id) ON DELETE SET NULL,
  target_url text,
  description text NOT NULL,
  status varchar(20) NOT NULL DEFAULT 'open',   -- open | reviewed | actioned | dismissed
  admin_notes text,
  resolved_by uuid FK admin_users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  resolved_at timestamptz
)
```

`users` also gains three plain columns this phase: `is_suspended boolean DEFAULT false`, `suspended_at timestamptz`, `suspended_reason varchar(500)` — admin "Suspend user" (roadmap §9.1), enforced at both `app/api/v1/deps.py::get_current_user` (dashboard) and `app/webhook/whatsapp/router.py` (WhatsApp, via a distinct decline reply — never a silently dropped message).

## Privacy Mode ↔ schema behavior

Retention periods are **config-driven, not hard-coded** (decisions.md §8) — every value below is a named environment variable (see `.env.example`), so changing a retention period is a config change, never an application-code change.

| | Privacy Mode **ON** (default) | Privacy Mode **OFF** |
|---|---|---|
| Explore | Excluded entirely — `explore_rollup.py` only reads `privacy_mode_snapshot=False` analyses | Included in the periodic rollup |
| Media (`media_attachments`) | Hard-deleted (row + blob) after `PRIVACY_MODE_MEDIA_TTL_MINUTES` post-processing | Standard retention, deletable on demand |
| Analysis rows | Hard-deleted by nightly purge after `PRIVACY_MODE_ANALYSIS_TTL_HOURS` — just long enough to deliver the reply + support resend | Standard retention (`STANDARD_RETENTION_MONTHS`, default), user-configurable shorter |
| History | NOT disabled — see Phase 6 deviation #2 below; works identically regardless of `privacy_mode` | Enabled |
| Scheduled Checks | NOT disabled either (a second, Phase 8 deviation from the original design) — `source_analysis_id SET NULL` (not CASCADE) lets a scheduled check outlive its source analysis being purged; a Privacy-Mode-ON user's underlying analysis just purges faster (~48h), which naturally starves same-user re-checks scheduled further out than that | Enabled |
| "Clear History" | N/A (nothing persists) | Always available — on-demand hard delete via cascading FKs |

**Full account deletion is a synchronous API call, not a queued `data_deletion_requests` table** — `DELETE /api/v1/account` (`app/agent/account_deletion.py`), gated by an exact `"DELETE"` confirmation-phrase match (judged the smallest correct solution over a second OTP round-trip). It writes an `audit_log` entry BEFORE anonymizing, explicitly deletes (child-to-parent, reusing Phase 6's `retention.py` helpers) every owned `scheduled_check` / `analysis`-and-its-children / `analysis_session` / `notification` / `otp_verification` / `dashboard_account` row, then anonymizes (not deletes) the `users` row itself — `phone_number_hash`/`phone_number_encrypted` re-derived from a fresh random UUID, `is_deleted=True`, `deleted_at` set, preferences reset to defaults — so the identifier can no longer be correlated post-deletion, satisfying a DPDP-style right-to-erasure rather than a soft-delete flag that leaves data recoverable. `audit_log.entity_id` has deliberately no FK to `users`, so the accountability record survives the anonymization. Setting `is_deleted=True` also immediately invalidates every outstanding JWT on its next authenticated request via the pre-existing `get_current_user` check — no separate token-revocation sweep was needed.

**Deletion jobs must be verified, not just implemented** (decisions.md §8): the purge job's effect needs an observable check (e.g. a Phase 5/6 integration test asserting a row + its blob are actually gone after the configured TTL elapses), not just code that's assumed to run correctly.

**These retention values are engineering defaults, not legally approved retention periods.** They are not to be described as DPDP-compliant, to users or otherwise, until the privacy/legal review in [decisions.md §9](decisions.md#9-dpdp--privacy--design-posture) is complete.

**Phase 6 implementation status, and two confirmed deviations from the table above**:

1. **Media/Analysis purge is real and verified** (`app/agent/retention.py`, `POST`-free — an in-process asyncio periodic task, run by `main.py`'s lifespan, NOT the Celery+Redis scheduler this document elsewhere assumes). Deletes are explicit, ordered, child-to-parent SQL statements rather than relying on `ON DELETE CASCADE` firing — this project's SQLite test engine never enables `PRAGMA foreign_keys`, so trusting CASCADE would have left the test suite unable to actually prove cascading deletion works. Verified by `backend/tests/integration/test_retention_purge.py` against a real (SQLite) test database: expired rows deleted, non-expired rows and other users' rows untouched, repeated runs idempotent, full parent+child trees (analysis → claims → evidence, media → transcripts/frames/forensics) provably gone with nothing orphaned.
2. **History is NOT disabled for Privacy Mode ON** (confirmed with the user, Phase 6 finalization — a discovered, disclosed conflict with this table's literal "Disabled" cell, not a silent deviation): `GET/DELETE /history` work identically regardless of `privacy_mode`. The intended effect of Privacy Mode is delivered entirely through the aggressive purge TTLs above, which already make a privacy-mode user's history naturally near-empty within ~48h — blocking the endpoint outright was judged to add no privacy benefit while breaking the ability to see an analysis that completed minutes ago. See `app/api/v1/routers/history.py`'s module docstring.
3. **"Clear History" is a SOFT delete** (`analyses.is_deleted = true`), not the "on-demand hard delete via cascading FKs" this table originally described — it reuses `analyses.is_deleted`/`deleted_at`, columns that already existed since Phase 2 for exactly this purpose. The row is subsequently HARD-deleted (with all children) once it reaches the purge's own age-based cutoff, same as any other analysis — user-initiated deletion currently only hides the row immediately, it does not accelerate the hard purge.

**Phase 8 addition**: full account erasure (the locked hard-delete-with-re-hashed-identifier design flagged as unbuilt above) IS now built — see "Full account deletion" above. It is a direct, synchronous cascade-and-anonymize call, not the `data_deletion_requests` queued-request table this document originally sketched.
