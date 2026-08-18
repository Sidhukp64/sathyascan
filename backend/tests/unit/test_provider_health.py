"""Unit tests for app/core/provider_health.py (Phase 9, roadmap §9.5)."""

from app.core.provider_health import ProviderHealthTracker


def test_record_success_populates_entry():
    tracker = ProviderHealthTracker()
    tracker.record_success("stt", latency_ms=42.5)

    snapshot = tracker.snapshot()
    entry = snapshot["stt"]
    assert entry.success_count == 1
    assert entry.failure_count == 0
    assert entry.consecutive_failures == 0
    assert entry.last_latency_ms == 42.5
    assert entry.last_success_at is not None


def test_record_failure_populates_entry_with_error_type_only():
    tracker = ProviderHealthTracker()
    tracker.record_failure("ocr", "TimeoutError")

    entry = tracker.snapshot()["ocr"]
    assert entry.failure_count == 1
    assert entry.consecutive_failures == 1
    assert entry.last_error_type == "TimeoutError"
    assert entry.last_failure_at is not None


def test_consecutive_failures_reset_on_success():
    tracker = ProviderHealthTracker()
    tracker.record_failure("llm", "ConnectionError")
    tracker.record_failure("llm", "ConnectionError")
    assert tracker.snapshot()["llm"].consecutive_failures == 2

    tracker.record_success("llm", 10.0)
    entry = tracker.snapshot()["llm"]
    assert entry.consecutive_failures == 0
    assert entry.failure_count == 2  # cumulative count is never reset
    assert entry.success_count == 1


def test_snapshot_is_a_copy_not_the_live_dict():
    tracker = ProviderHealthTracker()
    tracker.record_success("evidence_search")
    snapshot = tracker.snapshot()
    snapshot["evidence_search"].success_count = 999

    fresh_snapshot = tracker.snapshot()
    assert fresh_snapshot["evidence_search"].success_count == 1


def test_unknown_provider_key_starts_with_a_fresh_entry():
    tracker = ProviderHealthTracker()
    assert tracker.snapshot() == {}
    tracker.record_success("whatsapp")
    assert "whatsapp" in tracker.snapshot()
