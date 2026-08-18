"""
Wall-clock per-analysis timeout enforcement (decisions.md §12/§15: "Maximum
processing duration — a hard per-analysis wall-clock timeout, after which
the analysis fails gracefully into a PRD §38 error state rather than running
indefinitely"). Documented since Phase 2 via MAX_PROCESSING_DURATION_SECONDS
but never actually enforced anywhere in the codebase — Phase 5 closes this
gap, applied uniformly to every content-type pipeline (not just audio/video,
where a runaway frame-extraction/transcription job would matter most) via
app/webhook/whatsapp/router.py's background-task wrappers.

Callers wrap the ENTIRE `async with session_scope(...)` block, not just the
pipeline call, so a trip cancels mid-flight DB work too. SQLAlchemy's
AsyncSession rolls back any uncommitted work on unclean exit (see
app/db/session.py's session_scope: the outer `async with sessionmaker()`
closes/rolls back the session regardless of how its body exits), so a
timed-out attempt leaves no partial/inconsistent analysis row behind — it
simply never commits, as if the attempt hadn't started. The user still gets
a distinct, honest reply (never a silently dropped message, never a raw
error) — this is the timeout half of PRD §38's error-state requirement.
"""

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

T = TypeVar("T")


class PipelineTimeoutError(Exception):
    pass


async def run_with_timeout(coro: Coroutine[None, None, T], timeout_seconds: float) -> T:
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise PipelineTimeoutError() from exc
