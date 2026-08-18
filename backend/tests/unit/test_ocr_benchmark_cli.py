"""
Manifest loading and reporting for scripts/run_ocr_benchmark.py.

These paths only execute the day someone finally has a labelled corpus and a
credentialed engine — precisely when a silent bug is most expensive. The
loader's validation matters most: a mislabelled or empty ground truth would
not crash, it would quietly produce a wrong accuracy number and feed it into
a decisions.md §11 engine choice.
"""

import json

import pytest
from PIL import Image

from app.agent.ocr_benchmark import BenchmarkReport, LanguageScore, SampleScore, SampleOutcome
from scripts.run_ocr_benchmark import load_manifest, print_report


def _write_png(path):
    Image.new("RGB", (20, 10), "white").save(path)


def _manifest(tmp_path, samples, **extra):
    payload = {"name": "test-set", "samples": samples, **extra}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def test_loads_samples_and_resolves_paths_relative_to_the_manifest(tmp_path):
    (tmp_path / "ml").mkdir()
    _write_png(tmp_path / "ml" / "001.png")
    path = _manifest(tmp_path, [{"image": "ml/001.png", "language": "ml", "ground_truth": "കോട്ടയം"}])

    manifest, samples = load_manifest(path)

    assert manifest["name"] == "test-set"
    assert len(samples) == 1
    assert samples[0].language == "ml"
    assert samples[0].ground_truth == "കോട്ടയം"
    assert samples[0].mime_type == "image/png"
    assert samples[0].image_bytes.startswith(b"\x89PNG")


def test_ground_truth_file_is_read_as_utf8(tmp_path):
    """Long Indic ground truth belongs in a file, not hand-maintained inside
    JSON — and it must not be mangled to cp1252 on Windows."""
    _write_png(tmp_path / "a.png")
    (tmp_path / "a.txt").write_text("കോട്ടയം ജില്ല", encoding="utf-8")
    path = _manifest(tmp_path, [{"image": "a.png", "language": "ml", "ground_truth_file": "a.txt"}])

    _, samples = load_manifest(path)

    assert samples[0].ground_truth == "കോട്ടയം ജില്ല"


def test_sample_id_defaults_to_the_image_path(tmp_path):
    _write_png(tmp_path / "a.png")
    path = _manifest(tmp_path, [{"image": "a.png", "language": "en", "ground_truth": "hi"}])

    _, samples = load_manifest(path)

    assert samples[0].sample_id == "a.png"


def test_empty_ground_truth_is_rejected_rather_than_silently_scored(tmp_path):
    """A blank label scores every engine as perfect on that sample. Refusing
    it is the difference between a benchmark and a rubber stamp."""
    _write_png(tmp_path / "a.png")
    path = _manifest(tmp_path, [{"image": "a.png", "language": "ml", "ground_truth": "   "}])

    with pytest.raises(SystemExit, match="ground truth is empty"):
        load_manifest(path)


def test_missing_image_is_rejected(tmp_path):
    path = _manifest(tmp_path, [{"image": "nope.png", "language": "ml", "ground_truth": "x"}])

    with pytest.raises(SystemExit, match="image not found"):
        load_manifest(path)


def test_missing_ground_truth_key_is_rejected(tmp_path):
    _write_png(tmp_path / "a.png")
    path = _manifest(tmp_path, [{"image": "a.png", "language": "ml"}])

    with pytest.raises(SystemExit, match="ground_truth"):
        load_manifest(path)


def test_unsupported_image_type_is_rejected(tmp_path):
    (tmp_path / "a.bmp").write_bytes(b"BM-not-really")
    path = _manifest(tmp_path, [{"image": "a.bmp", "language": "ml", "ground_truth": "x"}])

    with pytest.raises(SystemExit, match="unsupported image type"):
        load_manifest(path)


def test_empty_manifest_is_rejected(tmp_path):
    with pytest.raises(SystemExit, match="no samples"):
        load_manifest(_manifest(tmp_path, []))


def _scorable_report():
    report = BenchmarkReport(tool_name="ocr", provider_name="p", model_version="v1")
    report.per_language = {
        "en": LanguageScore("en", scored_count=1, mean_cer=0.01, mean_wer=0.02),
        "ml": LanguageScore("ml", scored_count=1, mean_cer=0.42, mean_wer=0.55),
    }
    report.samples = [SampleScore("s1", "en", SampleOutcome.SCORED, 0.01, 0.02)]
    return report


def test_scorable_report_names_the_weakest_language(capsys):
    """The whole point of §11 — the report must say which language this
    engine is worst at, not just print a global average."""
    print_report(_scorable_report(), {"name": "test-set"})

    out = capsys.readouterr().out
    assert "Weakest language for this engine: ml" in out
    assert "0.4200" in out


def test_unscorable_report_refuses_to_present_itself_as_a_measurement(capsys):
    report = BenchmarkReport(tool_name="ocr", provider_name="null_stub", model_version=None)
    report.per_language = {"ml": LanguageScore("ml", provider_unavailable_count=2)}

    print_report(report, {"name": "test-set"})

    out = capsys.readouterr().out
    assert "NOT SCORABLE" in out
    assert "must not be recorded or cited" in out
    assert "Weakest language" not in out
