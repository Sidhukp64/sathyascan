"""
OCR accuracy benchmark engine (decisions.md §11).

§11 is a LOCKED sequencing rule: OCR/ASR quality is *not* assumed equal
across Malayalam, Tamil, Hindi and English, and an engine may not be locked
for production until it has been measured **per language**. That decision
also says the evaluation layer "is built" — this module is that layer's
scoring half. `app/agent/benchmarking.py` already persists a result; what was
missing was anything that actually produces one.

Nothing here invents a number. It runs whatever `OCRTool` it is handed over
labelled samples and reports what came back. With `NullOCRProvider` wired it
honestly reports a 100% provider-unavailable run, which is the correct
result for that configuration and not a failure of this code.

Two measurement decisions worth knowing before you read a report:

**Infrastructure failure is never averaged into accuracy.** An engine that
raises on 40% of Malayalam images is a categorically different problem from
one that returns wrong text, and averaging them hides exactly the per-language
weakness §11 exists to surface. Unavailable/failed samples are counted and
reported separately, never folded into CER/WER.

**CER here is code-point based, not grapheme based.** For Malayalam and Tamil
a human-perceived character is a grapheme cluster (base + combining marks),
so code-point CER penalises a single mis-rendered cluster more than once and
will read slightly pessimistic against these two scripts specifically.
Fixing it properly needs Unicode grapheme segmentation, which the stdlib does
not provide (`regex` or `uniseg` would be a new dependency). This is a stated
limitation of the metric, not a defect to be silently averaged away — compare
engines against each other on it, don't read an absolute CER as ground truth.
"""

import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from app.agent.tools.ocr import OCRStatus, OCRTool


class SampleOutcome(str, Enum):
    """Why a sample did or did not contribute to the accuracy figures."""

    SCORED = "scored"
    NO_TEXT_FOUND = "no_text_found"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class BenchmarkSample:
    """One labelled image. `language` is the ground-truth label, not a guess."""

    image_bytes: bytes
    mime_type: str
    ground_truth: str
    language: str
    sample_id: str


@dataclass(frozen=True)
class SampleScore:
    sample_id: str
    language: str
    outcome: SampleOutcome
    cer: float | None = None
    wer: float | None = None


@dataclass
class LanguageScore:
    """Per-language rollup — the unit §11 actually cares about."""

    language: str
    scored_count: int = 0
    no_text_found_count: int = 0
    provider_unavailable_count: int = 0
    failed_count: int = 0
    mean_cer: float | None = None
    mean_wer: float | None = None

    @property
    def total_count(self) -> int:
        return (
            self.scored_count
            + self.no_text_found_count
            + self.provider_unavailable_count
            + self.failed_count
        )


@dataclass
class BenchmarkReport:
    tool_name: str
    provider_name: str | None
    model_version: str | None
    samples: list[SampleScore] = field(default_factory=list)
    per_language: dict[str, LanguageScore] = field(default_factory=dict)

    @property
    def is_scorable(self) -> bool:
        """False when nothing produced a usable measurement — e.g. a Null
        provider. Callers must not present an unscorable run as an accuracy
        result."""
        return any(score.scored_count > 0 for score in self.per_language.values())

    def to_accuracy_metrics(self) -> dict:
        """Shape stored in `benchmark_runs.accuracy_metrics` (JSON column)."""
        return {
            "metric": "cer_wer_codepoint",
            "scorable": self.is_scorable,
            "provider_name": self.provider_name,
            "model_version": self.model_version,
            "per_language": {
                lang: {
                    "scored": s.scored_count,
                    "no_text_found": s.no_text_found_count,
                    "provider_unavailable": s.provider_unavailable_count,
                    "failed": s.failed_count,
                    "mean_cer": s.mean_cer,
                    "mean_wer": s.mean_wer,
                }
                for lang, s in sorted(self.per_language.items())
            },
        }


def normalize_for_scoring(text: str) -> str:
    """
    NFC-normalise and collapse whitespace before comparing.

    The NFC step is not cosmetic. Malayalam and Tamil routinely encode the
    same visible character as either a precomposed code point or a base plus
    combining mark; without normalisation two identical-looking strings score
    as a string of errors and every Indic CER becomes meaningless. Whitespace
    is collapsed because OCR line-breaking is a layout artefact, not a
    transcription error.
    """
    return " ".join(unicodedata.normalize("NFC", text).split())


def _levenshtein(reference: list | str, hypothesis: list | str) -> int:
    """Two-row Levenshtein — O(min) memory, so long transcripts stay cheap."""
    if reference == hypothesis:
        return 0
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)

    previous = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, start=1):
        current = [i]
        for j, hyp_item in enumerate(hypothesis, start=1):
            cost = 0 if ref_item == hyp_item else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    """
    Levenshtein edit distance over code points, divided by reference length.

    Can exceed 1.0 when the engine hallucinates text far longer than the
    reference — that is real information, so it is deliberately not clamped.
    """
    ref, hyp = normalize_for_scoring(reference), normalize_for_scoring(hypothesis)
    if not ref:
        # No reference to measure against; only an empty hypothesis is right.
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Same edit distance at word granularity.

    Worth reading alongside CER rather than instead of it: for the scripts
    here, whitespace-delimited "words" are a poor proxy for morphology —
    Malayalam is agglutinative, so one token can carry what English spreads
    over several. Treat WER as the weaker of the two signals.
    """
    ref = normalize_for_scoring(reference).split()
    hyp = normalize_for_scoring(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


_STATUS_TO_OUTCOME = {
    OCRStatus.NO_TEXT_FOUND: SampleOutcome.NO_TEXT_FOUND,
    OCRStatus.PROVIDER_UNAVAILABLE: SampleOutcome.PROVIDER_UNAVAILABLE,
    OCRStatus.FAILED: SampleOutcome.FAILED,
}


async def run_ocr_benchmark(tool: OCRTool, samples: list[BenchmarkSample]) -> BenchmarkReport:
    """
    Run `tool` over every sample and roll the results up per language.

    Sequential on purpose: this measures accuracy, not throughput, and firing
    a labelled corpus at a paid vision API concurrently is a good way to hit
    a rate limit and record provider-unavailable rows that look like an
    accuracy problem.
    """
    report = BenchmarkReport(tool_name="ocr", provider_name=None, model_version=None)
    cers: dict[str, list[float]] = {}
    wers: dict[str, list[float]] = {}

    for sample in samples:
        lang_score = report.per_language.setdefault(sample.language, LanguageScore(language=sample.language))
        result = await tool.extract(sample.image_bytes, sample.mime_type, sample.language)

        # Record identity from whatever actually answered, never assumed.
        if result.provider_name and report.provider_name is None:
            report.provider_name = result.provider_name
        if result.model_version and report.model_version is None:
            report.model_version = result.model_version

        if result.status is OCRStatus.SUCCESS and result.extracted_text is not None:
            cer = character_error_rate(sample.ground_truth, result.extracted_text)
            wer = word_error_rate(sample.ground_truth, result.extracted_text)
            cers.setdefault(sample.language, []).append(cer)
            wers.setdefault(sample.language, []).append(wer)
            lang_score.scored_count += 1
            report.samples.append(
                SampleScore(sample.sample_id, sample.language, SampleOutcome.SCORED, cer, wer)
            )
            continue

        outcome = _STATUS_TO_OUTCOME.get(result.status, SampleOutcome.FAILED)
        if outcome is SampleOutcome.NO_TEXT_FOUND:
            lang_score.no_text_found_count += 1
        elif outcome is SampleOutcome.PROVIDER_UNAVAILABLE:
            lang_score.provider_unavailable_count += 1
        else:
            lang_score.failed_count += 1
        report.samples.append(SampleScore(sample.sample_id, sample.language, outcome))

    for language, score in report.per_language.items():
        if score.scored_count:
            score.mean_cer = sum(cers[language]) / len(cers[language])
            score.mean_wer = sum(wers[language]) / len(wers[language])

    return report
