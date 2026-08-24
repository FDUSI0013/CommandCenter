"""Alembic environment.

Two things make this file different from the generated default:

* the URL comes from the application's settings, never from ``alembic.ini`` — the
  service and its migrations must agree on the database by construction;
* it runs the async engine, because the rest of the application is async and a
  second, sync driver dependency would be one more thing to install and pin.

``compare_type`` and ``compare_server_default`` are on so autogenerate notices a
column whose type or default drifted, not only ones that appeared or vanished.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from fulcrum_ops_api.core.config import settings
from fulcrum_ops_api.db.base import Base
from fulcrum_ops_api.db.session import create_engine

# Importing the model registry is what puts every table on Base.metadata.
import fulcrum_ops_api.models  # noqa: F401  (side-effect import)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # An explicit -x url=… wins, so a one-off migration against another database
    # never needs an environment variable exported first.
    return context.get_x_argument(as_dictionary=True).get("url") or settings.database_url


def _configure(connection) -> None:  # noqa: ANN001 — alembic passes a raw Connection
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of touching a database."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine: AsyncEngine = create_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(lambda sync_conn: (_configure(sync_conn), context.run_migrations()))
        await connection.commit()
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
