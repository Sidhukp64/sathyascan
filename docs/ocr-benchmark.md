# Running the OCR benchmark (decisions.md §11)

§11 is a locked sequencing rule: **no OCR engine may be locked for production until it has been measured per language** across Malayalam, Tamil, Hindi and English. "Supports 4 languages" has to be earned by measurement, not inherited from a vendor's feature list.

The tooling for this now exists and is tested. What does not exist is the data.

| Piece | Status |
|---|---|
| Scoring engine (`app/agent/ocr_benchmark.py`) | Built, 16 tests |
| CLI runner (`scripts/run_ocr_benchmark.py`) | Built, 10 tests |
| Persistence (`benchmark_datasets` / `benchmark_runs`) | Built since Phase 3 |
| **Labelled image corpus** | **Does not exist — this is the remaining work** |

Sourcing and labelling that corpus is human work. No amount of engineering removes it, which is exactly why §11 calls it a decision rather than a config step.

## Step 1 — Build the corpus

Aim for **at least 30 samples per language**, and make them look like what the bot will actually receive: WhatsApp screenshots of forwarded messages, photos of newspaper clippings, text-over-image graphics. A corpus of clean flatbed scans will measure something real, just not the thing this product does.

Layout — one directory, images plus a manifest:

```
corpus/
  manifest.json
  ml/001.png  ml/002.jpg  ...
  ta/001.png  ...
  hi/001.png  ...
  en/001.png  ...
```

`manifest.json`:

```json
{
  "name": "indic-ocr-v1",
  "source_description": "WhatsApp screenshots collected 2026-08, labelled by two native readers",
  "notes": "anything worth knowing when this run is read back later",
  "samples": [
    {"image": "ml/001.png", "language": "ml", "ground_truth": "കോട്ടയം ജില്ല"},
    {"image": "ta/001.png", "language": "ta", "ground_truth_file": "ta/001.txt"}
  ]
}
```

Use `ground_truth_file` for anything longer than one line — hand-maintaining a paragraph of Malayalam inside JSON is a mistake you make once.

**Labelling rules that decide whether the numbers mean anything:**

- Transcribe **exactly what is visible**, including typos in the source image. You are measuring transcription, not comprehension.
- Save every file as **UTF-8**. On Windows, Notepad's default "ANSI" will corrupt Indic text silently.
- Don't normalise by hand. The scorer NFC-normalises both sides, so precomposed vs. decomposed vowel signs already score as identical.
- Have a **native reader** label Malayalam and Tamil. This is the step where a benchmark quietly becomes worthless.
- Never leave a ground truth blank — the loader rejects it, because a blank label scores every engine as perfect on that sample.

## Step 2 — Run it, once per candidate engine

```bash
cd backend && .venv/Scripts/python.exe -m scripts.run_ocr_benchmark --manifest ../corpus/manifest.json
```

It uses whatever `OCR_PROVIDER` your `.env` names, so set that per engine and re-run. Output:

```
lang      n  scored      CER      WER  no-text  unavail  failed
---------------------------------------------------------------
en       30      30   0.0210   0.0450        0        0       0
ml       30      28   0.3180   0.4900        2        0       0
```

To persist a run for the audit trail (`benchmark_runs`), add `--record`. It refuses to record an unscorable run.

## Step 3 — Read it correctly

**Lower CER is better.** Two properties of this metric you must know before quoting a number:

**Infrastructure failure is never averaged into accuracy.** `unavail` and `failed` are counted separately and excluded from CER. An engine that crashes on 40% of Malayalam images is a different problem from one that returns wrong text, and averaging them hides precisely the per-language weakness §11 exists to expose. Read those columns first — a beautiful CER over 3 scored samples out of 30 is not a good result.

**CER is code-point based, not grapheme based.** For Malayalam and Tamil a human-perceived character is a grapheme cluster (base + combining marks), so one mis-rendered cluster can be counted as several errors. These figures read slightly **pessimistic against Malayalam and Tamil specifically**. Fixing it needs Unicode grapheme segmentation, which the stdlib lacks (`regex` or `uniseg` would be a new dependency). Use these numbers to compare engines **against each other**; do not publish an absolute CER as an accuracy claim.

WER is the weaker signal here — Malayalam is agglutinative, so whitespace-delimited "words" are a poor unit. Read it alongside CER, not instead of it.

## Step 4 — Make the decision

§11 is satisfied when you can say which engine you picked, per language, on measured evidence. One run measures one engine; it does not select one. Record the comparison, then update:

- `docs/feature-status-matrix.md` — the OCR row
- `docs/risks-and-open-questions.md` — the OCR/ASR accuracy section

Until then `NullOCRProvider` stays the default and `claude_vision` stays opt-in and explicitly unmeasured.

## Not covered

There is **no equivalent ASR benchmark runner**. §11 covers speech-to-text on the same terms, and Sarvam is in the same position OCR was — a real client, opt-in, never benchmarked. Building the ASR equivalent of this tooling is outstanding work.
