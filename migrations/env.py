"""Alembic environment (ADR-020).

The URL comes from ``DATABASE_URL`` (environment or .env) through core.config,
and logging goes through core.log, so the password is redacted in any output.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from core.config import get_settings
from core.db import NOT_SET_MESSAGE, DatabaseNotConfigured, engine_for
from core.log import configure_logging

settings = get_settings()
configure_logging(settings)

# Set when SQLAlchemy models exist; until then migrations are written by hand.
target_metadata = None


def run_migrations_offline() -> None:
    """``alembic upgrade --sql``: print SQL, no connection."""
    if settings.database_url is None:
        raise DatabaseNotConfigured(NOT_SET_MESSAGE)
    context.configure(
        url=settings.database_url.get_secret_value(),
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
    async with engine_for(settings, None) as engine, engine.connect() as connection:
        await connection.run_sync(_run)


# A caller (tests) may hand in a sync connection it controls, e.g. inside a
# transaction it will roll back: config.attributes["connection"].
_given_connection: Connection | None = context.config.attributes.get("connection")

if _given_connection is not None:
    _run(_given_connection)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
