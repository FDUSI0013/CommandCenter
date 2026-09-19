"""Async engine + session lifecycle.

SQLite (aiosqlite) is supported for local development and the test suite;
Postgres (asyncpg) is the deployed target. The only dialect-specific handling
lives here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ..core.config import settings

if TYPE_CHECKING:
    from starlette.background import BackgroundTasks

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_engine(url: str | None = None) -> AsyncEngine:
    url = url or settings.database_url
    kwargs: dict = {"echo": settings.database_echo, "future": True}
    if _is_sqlite(url):
        # SQLite has no meaningful server-side pooling; NullPool avoids
        # cross-task connection sharing surprises under asyncio.
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs.update(
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

    engine = create_async_engine(url, **kwargs)

    if _is_sqlite(url):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed on clean exit.

    "On exit" is later than it reads: after the response has been sent and after
    the route's ``BackgroundTasks`` have run. A route that hands a row it just
    wrote to a background job must use :func:`commit_then`.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def commit_then(
    session: AsyncSession,
    background: BackgroundTasks,
    job: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Make the request's writes durable, THEN schedule the job that reads them.

    ``get_session`` commits when the dependency exits, and under the FastAPI
    this service runs on a request-scoped ``yield`` dependency exits after the
    response has been sent -- which is after ``BackgroundTasks`` have run. So::

        run = await service.start_run(session, ...)     # row is only flushed
        background.add_task(service.execute_run, run.id, ...)

    starts a job that opens its own session, looks for a row no other
    connection can see yet, finds nothing and gives up: the run sits Queued
    until the stale-run sweep fails it. A route that hands a row to a
    background job calls this instead of ``background.add_task``::

        await commit_then(session, background, service.execute_run, run.id, ...)

    Call it last, after everything else in the route that can still fail: what
    is committed here stays committed, and a client holding the 202 is then
    holding a promise the database has already accepted. The dependency's own
    commit afterwards is a no-op, and the session stays usable for building the
    response (``expire_on_commit`` is off).
    """
    await session.commit()
    background.add_task(job, *args, **kwargs)


async def ping() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
