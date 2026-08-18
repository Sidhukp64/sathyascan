"""
Minimal benchmark-recording helper (decisions.md §2/§11). Infrastructure
only — see app/models/benchmark.py's docstring for why no real run has been
recorded in this build. Provided now so a future session with real OCR
credentials and a curated dataset can record results here without any
schema/plumbing work, and so the "before engine lock-in" sequencing rule is
mechanically enforceable (a query against benchmark_runs, not a promise).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark import BenchmarkRun


async def record_benchmark_run(
    session: AsyncSession,
    dataset_id,
    tool_name: str,
    model_version: str | None,
    accuracy_metrics: dict,
    notes: str | None = None,
) -> BenchmarkRun:
    from datetime import datetime, timezone

    run = BenchmarkRun(
        dataset_id=dataset_id,
        tool_name=tool_name,
        model_version=model_version,
        run_at=datetime.now(timezone.utc),
        accuracy_metrics=accuracy_metrics,
        notes=notes,
    )
    session.add(run)
    await session.flush()
    return run
