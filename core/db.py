"""Postgres access (ADR-011, ADR-020): async engine and a connection check.

One Postgres holds memory, runtime tasks and events. This module only builds the
engine and checks the connection; tables come with their features, through
Alembic migrations (``migrations/``).

Failures are reported as a ``DbStatus`` whose message is built from safe parts
only: host, port, database and user names, and exception type names. Never the
password, and never driver exception text, which can echo the connection string
or query parameters.

Check the database from a shell (fish), exit code 0 when reachable::

    uv run python -m core.db
"""

import asyncio
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.config import DATABASE_SCHEME, Settings, get_settings
from core.log import Redactor, secrets_from

CONNECT_TIMEOUT_S = 5.0

Problem = Literal[
    "ok",
    "not_configured",
    "misconfigured",
    "auth_failed",
    "database_missing",
    "unreachable",
    "timeout",
    "error",
]

# asyncpg exception class names (matched by name: asyncpg ships no type hints).
_AUTH_ERRORS = frozenset({"InvalidPasswordError", "InvalidAuthorizationSpecificationError"})
_MISSING_DB_ERRORS = frozenset({"InvalidCatalogNameError"})


class DatabaseConfigError(RuntimeError):
    """DATABASE_URL missing or malformed. The message never contains the URL."""


class DatabaseNotConfigured(DatabaseConfigError):
    pass


def url_problem(url: str) -> str | None:
    """Why ``url`` can't be used, or None. Never includes any part of the URL."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return "DATABASE_URL is not a valid URL"
    if parts.scheme != DATABASE_SCHEME:
        return f"DATABASE_URL must start with {DATABASE_SCHEME}://"
    if not host or not parts.path.strip("/"):
        return "DATABASE_URL needs a host and a database name"
    return None


@dataclass(frozen=True, slots=True)
class DbStatus:
    ok: bool
    problem: Problem
    message: str


def create_engine(settings: Settings) -> AsyncEngine:
    """Async engine for ``DATABASE_URL``. Does not connect yet."""
    if settings.database_url is None:
        raise DatabaseNotConfigured("DATABASE_URL is not set (see .env.example)")
    url = settings.database_url.get_secret_value()
    problem = url_problem(url)
    if problem is not None:
        raise DatabaseConfigError(problem)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        # Keep bound parameters (memory content, later) out of exception text.
        hide_parameters=True,
        connect_args={"timeout": CONNECT_TIMEOUT_S},
    )


def describe_target(settings: Settings) -> str:
    """``user@host:port/db`` without the password, for messages."""
    if settings.database_url is None:
        return "(not configured)"
    if url_problem(settings.database_url.get_secret_value()) is not None:
        return "(invalid DATABASE_URL)"
    parts = urlsplit(settings.database_url.get_secret_value())
    port = parts.port or 5432
    return f"{parts.username or '?'}@{parts.hostname}:{port}/{parts.path.lstrip('/')}"


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception, SQLAlchemy's wrapped ``orig``, and their causes/contexts."""
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        todo.extend(
            [
                orig if isinstance(orig, BaseException) else None,
                current.__cause__,
                current.__context__,
            ]
        )


def classify(exc: BaseException, target: str) -> DbStatus:
    chain = list(_chain(exc))
    names = {type(e).__name__ for e in chain}
    if names & _AUTH_ERRORS:
        return DbStatus(
            False,
            "auth_failed",
            f"Postgres rejected the login for {target}. Check that the password in "
            "DATABASE_URL matches POSTGRES_PASSWORD in .env. The container sets the "
            "password only when its data volume is first created.",
        )
    if names & _MISSING_DB_ERRORS:
        return DbStatus(False, "database_missing", f"database does not exist: {target}")
    if any(isinstance(e, TimeoutError) for e in chain):
        return DbStatus(
            False, "timeout", f"no answer from Postgres at {target} within {CONNECT_TIMEOUT_S}s"
        )
    if any(isinstance(e, OSError) for e in chain):
        return DbStatus(
            False,
            "unreachable",
            f"cannot reach Postgres at {target}. Is the container running? (docker compose ps)",
        )
    return DbStatus(False, "error", f"database error at {target} ({type(exc).__name__})")


async def check_database(settings: Settings, engine: AsyncEngine | None = None) -> DbStatus:
    """Connect and run ``SELECT 1``. Never raises (except cancellation)."""
    if settings.database_url is None:
        return DbStatus(False, "not_configured", "DATABASE_URL is not set (see .env.example)")
    problem = url_problem(settings.database_url.get_secret_value())
    if problem is not None:
        return DbStatus(False, "misconfigured", problem)
    target = describe_target(settings)
    own_engine = engine is None
    engine = engine if engine is not None else create_engine(settings)
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_S + 1):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return classify(exc, target)
    finally:
        if own_engine:
            await engine.dispose()
    return DbStatus(True, "ok", f"connected to {target}")


def main() -> int:
    settings = get_settings()
    status = asyncio.run(check_database(settings))
    # Messages are built from safe parts; redact anyway as a second layer.
    print(Redactor(secrets_from(settings))(f"{status.problem}: {status.message}"))
    return 0 if status.ok else 1


if __name__ == "__main__":
    sys.exit(main())
