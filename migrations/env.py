"""Alembic environment (ADR-020).

The URL is the owner's: ``DATABASE_MIGRATION_URL``, else ``DATABASE_URL``
(environment or .env, through core.config; see ``core.db.migration_url``). Core
itself connects as the least-privilege app role (#1, #12). Logging goes through
core.log, so both passwords are redacted in any output.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from core.config import get_settings
from core.db import (
    NOT_SET_MESSAGE,
    DatabaseConfigError,
    DatabaseNotConfigured,
    create_migration_engine,
    migration_url,
    url_problem,
)
from core.log import configure_logging

settings = get_settings()
configure_logging(settings)

# Migrations are written by hand and autogenerate is not used, so no metadata is
# set. Tests check the code against the migrations instead (test_events.py,
# test_memory_store.py).
target_metadata = None


def run_migrations_offline() -> None:
    """``alembic upgrade --sql``: print SQL, no connection."""
    name, url = migration_url(settings)
    if url is None:
        raise DatabaseNotConfigured(NOT_SET_MESSAGE)
    problem = url_problem(url.get_secret_value(), name)
    if problem is not None:
        raise DatabaseConfigError(problem)
    context.configure(
        url=url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_migration_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


# A caller (tests) may hand in a sync connection it controls, e.g. inside a
# transaction it will roll back: config.attributes["connection"].
_given_connection: Connection | None = context.config.attributes.get("connection")

if _given_connection is not None:
    _run(_given_connection)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
