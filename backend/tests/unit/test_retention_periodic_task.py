"""
Unit tests for app/main.py's `_run_periodic_retention_purge` — the
in-process asyncio loop that calls app/agent/retention.run_retention_purge
on a fixed interval (main.py's lifespan, NOT Celery — see that function's
docstring). Runs the loop directly with a very short interval and a fake
app/settings, rather than booting the whole FastAPI app, since this is pure
scheduling-and-error-handling logic independent of the HTTP surface.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.main import _run_periodic_retention_purge


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.asyncio
async def test_purge_loop_calls_run_retention_purge_on_each_tick(monkeypatch):
    call_count = 0

    async def fake_run_retention_purge(session, settings):
        nonlocal call_count
        call_count += 1
        from app.agent.retention import PurgeResult

        return PurgeResult(media_attachments_deleted=0, analyses_deleted=0)

    monkeypatch.setattr("app.main.run_retention_purge", fake_run_retention_purge)
    monkeypatch.setattr("app.main.session_scope", lambda sessionmaker: _FakeSessionCtx())

    fake_app = SimpleNamespace(state=SimpleNamespace(db_sessionmaker=object()))
    fake_settings = SimpleNamespace(retention_purge_interval_seconds=0.01)

    task = asyncio.create_task(_run_periodic_retention_purge(fake_app, fake_settings))
    await asyncio.sleep(0.05)  # several ticks at a 0.01s interval
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert call_count >= 2  # proves the loop actually re-fires, not a one-shot


@pytest.mark.asyncio
async def test_purge_loop_survives_an_exception_and_keeps_running(monkeypatch):
    call_count = 0

    async def flaky_run_retention_purge(session, settings):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated transient DB error")
        from app.agent.retention import PurgeResult

        return PurgeResult(media_attachments_deleted=0, analyses_deleted=0)

    monkeypatch.setattr("app.main.run_retention_purge", flaky_run_retention_purge)
    monkeypatch.setattr("app.main.session_scope", lambda sessionmaker: _FakeSessionCtx())

    fake_app = SimpleNamespace(state=SimpleNamespace(db_sessionmaker=object()))
    fake_settings = SimpleNamespace(retention_purge_interval_seconds=0.01)

    task = asyncio.create_task(_run_periodic_retention_purge(fake_app, fake_settings))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The FIRST tick raised — the loop must have continued to a second,
    # successful tick rather than dying silently.
    assert call_count >= 2
