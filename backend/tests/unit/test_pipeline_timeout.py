"""
run_with_timeout tests (decisions.md §12/§15 — the wall-clock timeout that
was documented since Phase 2 but never actually enforced until Phase 5).
"""

import asyncio

import pytest

from app.core.pipeline_timeout import PipelineTimeoutError, run_with_timeout


async def _slow(delay_seconds: float, result: str = "done") -> str:
    await asyncio.sleep(delay_seconds)
    return result


class TestRunWithTimeout:
    async def test_fast_coroutine_completes_normally(self):
        result = await run_with_timeout(_slow(0.01, "ok"), timeout_seconds=5.0)
        assert result == "ok"

    async def test_slow_coroutine_raises_pipeline_timeout_error(self):
        with pytest.raises(PipelineTimeoutError):
            await run_with_timeout(_slow(1.0), timeout_seconds=0.05)

    async def test_exception_from_the_wrapped_coroutine_propagates_unchanged(self):
        async def _raises():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await run_with_timeout(_raises(), timeout_seconds=5.0)
