"""
Run the decisions.md §11 per-language OCR benchmark against a labelled set.

    python -m scripts.run_ocr_benchmark --manifest path/to/manifest.json
    python -m scripts.run_ocr_benchmark --manifest ... --record   # persist

This is the gate that stands between "an OCR engine is wired" and "an OCR
engine is *selected*". It uses whatever provider your `.env` configures, so
run it once per candidate engine and compare — that comparison, not this
script, is the actual §11 decision.

Manifest (paths resolve relative to the manifest file):

    {
      "name": "indic-ocr-v1",
      "source_description": "where these samples came from and how labelled",
      "samples": [
        {"image": "ml/001.jpg", "language": "ml", "ground_truth": "..."},
        {"image": "ta/002.png", "language": "ta", "ground_truth_file": "ta/002.txt"}
      ]
    }

Use `ground_truth_file` for anything longer than a line — JSON is a poor
place to hand-maintain a paragraph of Malayalam.

This script cannot manufacture the samples. Sourcing and labelling real
Malayalam/Tamil/Hindi/English images is human work, and it is the entire
remaining cost of the §11 gate.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.agent.ocr_benchmark import BenchmarkSample, run_ocr_benchmark  # noqa: E402
from app.agent.tools.ocr import OCRTool  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.provider_selection import build_ocr_provider  # noqa: E402
from app.integrations.claude_client import AnthropicLLMClient  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - a cp1252 console must not break the run
    pass

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def load_manifest(manifest_path: Path) -> tuple[dict, list[BenchmarkSample]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    samples: list[BenchmarkSample] = []

    for index, entry in enumerate(manifest.get("samples", [])):
        image_path = (root / entry["image"]).resolve()
        if not image_path.is_file():
            raise SystemExit(f"sample {index}: image not found: {image_path}")

        suffix = image_path.suffix.lower()
        if suffix not in _MIME_BY_SUFFIX:
            raise SystemExit(f"sample {index}: unsupported image type {suffix!r}")

        if "ground_truth_file" in entry:
            ground_truth = (root / entry["ground_truth_file"]).read_text(encoding="utf-8")
        elif "ground_truth" in entry:
            ground_truth = entry["ground_truth"]
        else:
            raise SystemExit(f"sample {index}: needs ground_truth or ground_truth_file")

        if not ground_truth.strip():
            # A blank label silently scores as a perfect miss; refuse it.
            raise SystemExit(f"sample {index}: ground truth is empty")

        samples.append(
            BenchmarkSample(
                image_bytes=image_path.read_bytes(),
                mime_type=_MIME_BY_SUFFIX[suffix],
                ground_truth=ground_truth,
                language=entry["language"],
                sample_id=entry.get("id", entry["image"]),
            )
        )

    if not samples:
        raise SystemExit("manifest contains no samples")
    return manifest, samples


def print_report(report, manifest: dict) -> None:
    print(f"\nOCR benchmark — {manifest.get('name', '(unnamed dataset)')}")
    print(f"provider: {report.provider_name or '(none reported)'}   "
          f"model: {report.model_version or '(none reported)'}\n")

    header = f"{'lang':<6}{'n':>5}{'scored':>8}{'CER':>9}{'WER':>9}{'no-text':>9}{'unavail':>9}{'failed':>8}"
    print(header)
    print("-" * len(header))
    for language in sorted(report.per_language):
        s = report.per_language[language]
        cer = f"{s.mean_cer:.4f}" if s.mean_cer is not None else "—"
        wer = f"{s.mean_wer:.4f}" if s.mean_wer is not None else "—"
        print(f"{language:<6}{s.total_count:>5}{s.scored_count:>8}{cer:>9}{wer:>9}"
              f"{s.no_text_found_count:>9}{s.provider_unavailable_count:>9}{s.failed_count:>8}")

    print()
    if not report.is_scorable:
        print("NOT SCORABLE — no sample produced text. With OCR_PROVIDER unset this is")
        print("the expected result (NullOCRProvider). It is not an accuracy measurement")
        print("and must not be recorded or cited as one.")
        return

    print("Lower CER is better. decisions.md §11 requires comparing candidate engines")
    print("per language — a single run measures one engine, it does not select one.")
    weakest = max(
        (s for s in report.per_language.values() if s.mean_cer is not None),
        key=lambda s: s.mean_cer,
    )
    print(f"Weakest language for this engine: {weakest.language} (CER {weakest.mean_cer:.4f})")


async def record(report, manifest: dict, sample_count: int) -> None:
    """Persist to benchmark_datasets/benchmark_runs via the existing helper."""
    from app.agent.benchmarking import record_benchmark_run
    from app.db.session import build_engine, build_sessionmaker
    from app.models.benchmark import BenchmarkDataset

    if not report.is_scorable:
        raise SystemExit("refusing to record an unscorable run — see the note above")

    engine = build_engine(get_settings().database_url)
    sessionmaker = build_sessionmaker(engine)
    async with sessionmaker() as session:
        dataset = BenchmarkDataset(
            name=manifest.get("name", "unnamed"),
            modality="ocr",
            language=None,  # multi-language set; per-language detail lives in the run
            sample_count=sample_count,
            source_description=manifest.get("source_description"),
        )
        session.add(dataset)
        await session.flush()

        run = await record_benchmark_run(
            session,
            dataset_id=dataset.id,
            tool_name="ocr",
            model_version=report.model_version,
            accuracy_metrics=report.to_accuracy_metrics(),
            notes=manifest.get("notes"),
        )
        await session.commit()
        print(f"\nRecorded benchmark_run {run.id} (dataset {dataset.id})")
    await engine.dispose()


async def main_async(args) -> None:
    manifest, samples = load_manifest(Path(args.manifest).resolve())

    settings = get_settings()
    llm = AnthropicLLMClient(settings.anthropic_api_key, settings.anthropic_model)
    tool = OCRTool(build_ocr_provider(settings, llm))

    print(f"Running {len(samples)} samples through OCR_PROVIDER={settings.ocr_provider or '(unset -> Null)'} ...")
    report = await run_ocr_benchmark(tool, samples)
    print_report(report, manifest)

    if args.record:
        await record(report, manifest, len(samples))


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-language OCR benchmark (decisions.md §11)")
    parser.add_argument("--manifest", required=True, help="path to the dataset manifest JSON")
    parser.add_argument("--record", action="store_true", help="persist the result to benchmark_runs")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
