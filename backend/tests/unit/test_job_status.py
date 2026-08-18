"""Unit tests for app/core/job_status.py (Phase 9, roadmap §9.5/§9.1)."""

from app.core.job_status import JobStatusTracker


def test_record_tick_success_populates_entry():
    tracker = JobStatusTracker()
    tracker.record_tick_success("retention_purge")

    entry = tracker.snapshot()["retention_purge"]
    assert entry.run_count == 1
    assert entry.failure_count == 0
    assert entry.last_run_at is not None
    assert entry.last_success_at is not None
    assert entry.last_error is None


def test_record_tick_failure_populates_entry_with_error_type_only():
    tracker = JobStatusTracker()
    tracker.record_tick_failure("explore_rollup", "OperationalError")

    entry = tracker.snapshot()["explore_rollup"]
    assert entry.run_count == 1
    assert entry.failure_count == 1
    assert entry.last_error == "OperationalError"
    # A failed tick still updates last_run_at (it DID run), but never
    # last_success_at (it did NOT succeed).
    assert entry.last_run_at is not None
    assert entry.last_success_at is None


def test_run_count_accumulates_across_success_and_failure():
    tracker = JobStatusTracker()
    tracker.record_tick_success("scheduled_check_runner")
    tracker.record_tick_failure("scheduled_check_runner", "RuntimeError")
    tracker.record_tick_success("scheduled_check_runner")

    entry = tracker.snapshot()["scheduled_check_runner"]
    assert entry.run_count == 3
    assert entry.failure_count == 1
