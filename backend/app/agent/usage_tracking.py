"""
Daily spend circuit breaker (decisions.md §1). One usage_ledger row per UTC
calendar date. Checked BEFORE a new analysis starts (no LLM/search call is
made if the breaker is already tripped) and updated AFTER an analysis
finishes, using the real counters accumulated on the Analysis row.
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage_ledger import UsageLedger


def _today() -> date:
    return datetime.now(timezone.utc).date()


async def _get_or_create_today(session: AsyncSession) -> UsageLedger:
    result = await session.execute(select(UsageLedger).where(UsageLedger.date == _today()))
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    row = UsageLedger(date=_today())
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # Another concurrent request created today's row first — fetch it.
        await session.rollback()
        result = await session.execute(select(UsageLedger).where(UsageLedger.date == _today()))
        row = result.scalar_one()
    return row


async def is_circuit_breaker_tripped(session: AsyncSession, daily_spend_limit_usd: float) -> bool:
    row = await _get_or_create_today(session)
    if row.circuit_breaker_tripped:
        return True
    return float(row.estimated_cost_usd) >= daily_spend_limit_usd


async def record_usage(
    session: AsyncSession,
    evidence_search_count: int,
    llm_tool_call_count: int,
    cost_per_search_usd: float,
    cost_per_llm_call_usd: float,
    daily_spend_limit_usd: float,
) -> None:
    row = await _get_or_create_today(session)
    row.evidence_search_count += evidence_search_count
    row.llm_tool_call_count += llm_tool_call_count
    row.estimated_cost_usd = float(row.estimated_cost_usd) + (
        evidence_search_count * cost_per_search_usd + llm_tool_call_count * cost_per_llm_call_usd
    )
    if not row.circuit_breaker_tripped and float(row.estimated_cost_usd) >= daily_spend_limit_usd:
        row.circuit_breaker_tripped = True
        row.tripped_at = datetime.now(timezone.utc)
    await session.flush()
