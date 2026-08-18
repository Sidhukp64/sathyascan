"""
usage_ledger — docs/database-schema.md, decisions.md §1.

Backs the daily spend circuit breaker. One row per calendar date (UTC).
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class UsageLedger(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "usage_ledger"

    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    evidence_search_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_tool_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    circuit_breaker_tripped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
