"""
OCR benchmark engine (app/agent/ocr_benchmark.py) — the decisions.md §11
per-language measurement layer.

These tests pin the scoring behaviour that a wrong benchmark would get
quietly wrong: Indic Unicode normalisation, and the refusal to average
infrastructure failures into an accuracy score. A benchmark that is itself
subtly wrong is worse than no benchmark, because §11 makes a production
engine choice on its output.
"""

import unicodedata

import pytest

from app.agent.ocr_benchmark import (
    BenchmarkSample,
    SampleOutcome,
    character_error_rate,
    normalize_for_scoring,
    run_ocr_benchmark,
    word_error_rate,
)
from app.agent.tools.ocr import (
    NullOCRProvider,
    OCRProviderUnavailable,
    OCRResult,
    OCRStatus,
    OCRTool,
)

# "കോട്ടയം" (Kottayam) and "கோவை" (Kovai) — both carry a vowel sign with a
# real canonical decomposition (Malayalam U+0D4B -> U+0D47 U+0D3E, Tamil
# U+0BCB -> U+0BC7 U+0BBE), so NFC and NFD genuinely differ here. An OCR
# engine and a hand-typed ground-truth file routinely disagree on exactly
# this while rendering identically on screen.
#
# Note most Malayalam/Tamil vowel signs are atomic and have NO decomposition
# (U+0D47 in "കേരളം" is one), so only a word built on the composed O/AU
# signs actually exercises normalisation.
MALAYALAM_NFC = unicodedata.normalize("NFC", "കോട്ടയം")
MALAYALAM_NFD = unicodedata.normalize("NFD", "കോട്ടയം")
TAMIL_NFC = unicodedata.normalize("NFC", "கோவை")
TAMIL_NFD = unicodedata.normalize("NFD", "கோவை")


class ScriptedOCR:
    """Returns a queued result per call, so a mixed-outcome run is testable."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def extract_text(self, image_bytes, mime_type, language_hint):
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _sample(sample_id="s1", language="ml", ground_truth="hello"):
    return BenchmarkSample(
        image_bytes=b"\xff\xd8\xff\xe0img",
        mime_type="image/jpeg",
        ground_truth=ground_truth,
        language=language,
        sample_id=sample_id,
    )


def _success(text):
    return OCRResult(
        status=OCRStatus.SUCCESS,
        extracted_text=text,
        provider_name="scripted",
        model_version="v1",
    )


# --------------------------------------------------------------- metrics


def test_identical_text_scores_zero_error():
    assert character_error_rate("hello world", "hello world") == 0.0
    assert word_error_rate("hello world", "hello world") == 0.0


@pytest.mark.parametrize(
    "nfc,nfd",
    [(MALAYALAM_NFC, MALAYALAM_NFD), (TAMIL_NFC, TAMIL_NFD)],
    ids=["malayalam", "tamil"],
)
def test_nfd_and_nfc_indic_text_is_scored_as_identical(nfc, nfd):
    """The single most important property of this metric. Without NFC
    normalisation these two visually identical strings score as a run of
    errors and every Malayalam/Tamil CER becomes meaningless."""
    assert nfc != nfd  # genuinely different code points
    assert character_error_rate(nfc, nfd) == 0.0
    assert normalize_for_scoring(nfd) == normalize_for_scoring(nfc)


def test_line_break_differences_are_not_counted_as_errors():
    """OCR line-wrapping is a layout artefact of the image, not a
    transcription mistake."""
    assert character_error_rate("the quick brown fox", "the quick\nbrown   fox") == 0.0


def test_cer_reflects_actual_edit_distance():
    # "kitten" -> "sitting" is the textbook distance-3 case; 3/6 = 0.5.
    assert character_error_rate("kitten", "sitting") == pytest.approx(0.5)


def test_word_error_rate_counts_whole_word_substitutions():
    assert word_error_rate("the cat sat", "the dog sat") == pytest.approx(1 / 3)


def test_completely_wrong_output_scores_at_or_above_one():
    assert character_error_rate("abc", "xyz") == pytest.approx(1.0)


def test_hallucinated_extra_text_is_not_clamped_to_one():
    """An engine inventing text well beyond the reference is worse than one
    returning nothing, and the number must be able to say so."""
    assert character_error_rate("hi", "hi and a great deal of invented text") > 1.0


def test_empty_reference_is_handled_without_dividing_by_zero():
    assert character_error_rate("", "") == 0.0
    assert character_error_rate("", "spurious") == 1.0
    assert word_error_rate("", "spurious") == 1.0


# ----------------------------------------------------------- benchmark run


@pytest.mark.asyncio
async def test_scores_are_rolled_up_per_language_not_globally():
    """§11 exists because per-language quality differs; a single global mean
    would hide exactly the weakness it is meant to expose."""
    tool = OCRTool(ScriptedOCR([
        _success("perfect"),      # ml, exact
        _success("wrong text"),   # ta, wrong
    ]))
    report = await run_ocr_benchmark(tool, [
        _sample("ml-1", "ml", "perfect"),
        _sample("ta-1", "ta", "correct text"),
    ])

    assert report.per_language["ml"].mean_cer == 0.0
    assert report.per_language["ta"].mean_cer > 0.0
    assert report.is_scorable


@pytest.mark.asyncio
async def test_provider_failures_are_excluded_from_accuracy_not_scored_as_zero():
    """A crashed sample must not be averaged in as either a perfect score or
    a total miss — it is an infrastructure fact, not an accuracy fact."""
    tool = OCRTool(ScriptedOCR([
        _success("perfect"),
        OCRProviderUnavailable("api down"),
    ]))
    report = await run_ocr_benchmark(tool, [
        _sample("ml-1", "ml", "perfect"),
        _sample("ml-2", "ml", "never measured"),
    ])

    score = report.per_language["ml"]
    assert score.scored_count == 1
    assert score.provider_unavailable_count == 1
    assert score.total_count == 2
    # The mean reflects ONLY the sample that actually produced text.
    assert score.mean_cer == 0.0


@pytest.mark.asyncio
async def test_no_text_found_is_tracked_separately_from_a_hard_failure():
    tool = OCRTool(ScriptedOCR([OCRResult(status=OCRStatus.NO_TEXT_FOUND)]))
    report = await run_ocr_benchmark(tool, [_sample("ml-1", "ml", "some text")])

    score = report.per_language["ml"]
    assert score.no_text_found_count == 1
    assert score.provider_unavailable_count == 0
    assert report.samples[0].outcome is SampleOutcome.NO_TEXT_FOUND
    assert report.samples[0].cer is None  # never invented for an unscored sample


@pytest.mark.asyncio
async def test_null_provider_run_is_reported_as_not_scorable():
    """The honest result for an unconfigured system. `is_scorable` is what
    stops a Null run being presented as a §11 measurement."""
    report = await run_ocr_benchmark(
        OCRTool(NullOCRProvider()),
        [_sample("ml-1", "ml", "text"), _sample("ta-1", "ta", "text")],
    )

    assert report.is_scorable is False
    assert all(s.cer is None for s in report.samples)
    assert report.to_accuracy_metrics()["scorable"] is False


@pytest.mark.asyncio
async def test_provider_identity_is_read_from_the_result_never_assumed():
    tool = OCRTool(ScriptedOCR([_success("perfect")]))
    report = await run_ocr_benchmark(tool, [_sample("ml-1", "ml", "perfect")])

    assert report.provider_name == "scripted"
    assert report.model_version == "v1"


@pytest.mark.asyncio
async def test_accuracy_metrics_payload_is_json_serialisable_per_language():
    """It lands in a JSON column, so it has to survive the round trip."""
    import json

    tool = OCRTool(ScriptedOCR([_success("perfect"), _success("x")]))
    report = await run_ocr_benchmark(tool, [
        _sample("ml-1", "ml", "perfect"),
        _sample("hi-1", "hi", "different"),
    ])

    payload = json.loads(json.dumps(report.to_accuracy_metrics()))
    assert payload["metric"] == "cer_wer_codepoint"
    assert set(payload["per_language"]) == {"ml", "hi"}
    assert payload["per_language"]["ml"]["mean_cer"] == 0.0


@pytest.mark.asyncio
async def test_language_label_is_passed_to_the_provider_as_the_hint():
    """The ground-truth label doubles as the language hint, so an engine that
    supports hinting is measured under the conditions it will run in."""
    scripted = ScriptedOCR([_success("perfect")])
    captured = {}

    original = scripted.extract_text

    async def spy(image_bytes, mime_type, language_hint):
        captured["hint"] = language_hint
        return await original(image_bytes, mime_type, language_hint)

    scripted.extract_text = spy
    await run_ocr_benchmark(OCRTool(scripted), [_sample("ta-1", "ta", "perfect")])

    assert captured["hint"] == "ta"
