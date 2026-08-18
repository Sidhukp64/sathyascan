"""
Async SQLAlchemy engine/session factory.

Production/dev target is PostgreSQL (asyncpg driver, per docs/decisions.md
lean-infra choice). Tests use in-memory SQLite (aiosqlite) instead of a real
Postgres server — there is no Postgres available in this dev sandbox, and
SQLite-for-tests / Postgres-for-real is a standard, well-accepted pattern for
a schema with no Postgres-only types (this Phase 2 subset has none — no
pgvector, no citext). See tests/conftest.py for the override.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def build_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Used by background tasks, which run outside FastAPI's per-request
    dependency injection lifecycle and so need to open/close their own
    session explicitly."""
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
