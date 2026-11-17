"""Postgres access (ADR-011, ADR-020): async engine and a connection check.

One Postgres holds memory, runtime tasks and events. This module only builds the
engine and checks the connection; tables come with their features, through
Alembic migrations (``migrations/``).

Two roles (#1, #12, ADR-020 addition 2026-11-16). Core connects with
``DATABASE_URL``, the least-privilege app role (``aether_app``, created by
migration 0003). Alembic connects with ``DATABASE_MIGRATION_URL``, the owner, and
falls back to ``DATABASE_URL`` when it is unset. Every new connection of Core's
engine is checked: a role that is a superuser, owns the database or ``events``,
or can create objects is refused (``PrivilegedRoleError``), so Core can't run
as the owner by mistake.

Failures are reported as a ``DbStatus`` whose message is built from safe parts
only: host, port, database and user names, and exception type names. Never the
password, and never driver exception text, which can echo the connection string
or query parameters.

Check the database from a shell (fish), exit code 0 when reachable::

    uv run python -m core.db
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.config import DATABASE_SCHEME, Settings, get_settings
from core.log import MIN_SECRET_LENGTH, Redactor, secrets_from

CONNECT_TIMEOUT_S = 5.0
NOT_SET_MESSAGE = "DATABASE_URL is not set (see .env.example)"
# Shorter passwords aren't redacted by value in logs (core.log), so they're refused.
MIN_PASSWORD_LENGTH = MIN_SECRET_LENGTH
RUNBOOK = "docs/runbooks/db-app-role.md"
PRIVILEGED_MESSAGE = (
    "DATABASE_URL connects as a privileged role (superuser, owner, or able to create "
    "objects). Core must use the least-privilege app role; the owner role belongs in "
    f"DATABASE_MIGRATION_URL. See {RUNBOOK}"
)
SAME_ROLE_MESSAGE = (
    "DATABASE_URL and DATABASE_MIGRATION_URL use the same role. Core must use the "
    f"least-privilege app role. See {RUNBOOK}"
)

# True when the connected role could change or drop what it should only append to:
# special attributes; owning the database or any non-system table or function;
# CREATE on the database or any non-system schema; predefined roles that write
# any table or reach the server's files or programs; TRUNCATE or TRIGGER on any
# non-system table; or UPDATE (on the table or any column) or DELETE on any
# non-system table except ``memory_records`` (MUTABLE_TABLE: Core corrects and
# hard-deletes memory records, ADR-024 §4; migration 0004). The exception is that
# one table name and those two rights only: ``events`` and ``alembic_version``
# stay insert/read-only, and owning or creating anything is refused everywhere.
# Independent of search_path. Each part is COALESCEd: a NULL must not hide the rest.
MUTABLE_TABLE = "memory_records"
PRIVILEGE_QUERY = """
SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
    OR r.rolbypassrls
    OR pg_has_role(current_user, d.datdba, 'MEMBER')
    OR has_database_privilege(current_user, d.oid, 'CREATE')
    OR pg_has_role(current_user, 'pg_write_all_data', 'MEMBER')
    OR pg_has_role(current_user, 'pg_write_server_files', 'MEMBER')
    OR pg_has_role(current_user, 'pg_execute_server_program', 'MEMBER')
    OR pg_has_role(current_user, 'pg_read_server_files', 'MEMBER')
    OR EXISTS (
        SELECT 1 FROM pg_namespace n
        WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
          AND has_schema_privilege(current_user, n.oid, 'CREATE'))
    OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
          AND (pg_has_role(current_user, c.relowner, 'MEMBER')
               OR (c.relkind IN ('r', 'p')
                   AND (has_table_privilege(current_user, c.oid, 'TRUNCATE, TRIGGER')
                        OR (c.relname <> 'memory_records'
                            AND (has_table_privilege(current_user, c.oid, 'UPDATE, DELETE')
                                 OR has_any_column_privilege(current_user, c.oid,
                                                             'UPDATE')))))))
    OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
          AND pg_has_role(current_user, p.proowner, 'MEMBER'))
FROM pg_roles r, pg_database d
WHERE r.rolname = current_user AND d.datname = current_database()
"""

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


class PrivilegedRoleError(DatabaseConfigError):
    """Core's ``DATABASE_URL`` is the owner or another privileged role. Core refuses
    to start (core.app). The message never contains any part of a URL."""


def url_problem(url: str, name: str = "DATABASE_URL") -> str | None:
    """Why ``url`` can't be used, or None. Never includes any part of the URL;
    ``name`` is the setting's name, for the message."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
        password = parts.password
    except ValueError:
        return f"{name} is not a valid URL"
    if parts.scheme != DATABASE_SCHEME:
        return f"{name} must start with {DATABASE_SCHEME}://"
    if not host or not parts.path.strip("/"):
        return f"{name} needs a host and a database name"
    if not parts.username:
        return f"{name} needs a user name"
    if len(unquote(password or "")) < MIN_PASSWORD_LENGTH:
        return f"{name} needs a password of at least {MIN_PASSWORD_LENGTH} characters"
    return None


def _username(url: SecretStr) -> str | None:
    try:
        return urlsplit(url.get_secret_value()).username
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DbStatus:
    ok: bool
    problem: Problem
    message: str


def url_status(settings: Settings) -> DbStatus | None:
    """Why ``DATABASE_URL`` can't be used (``not_configured`` / ``misconfigured``), or
    None if it is set and well-formed. The message never contains any part of the URL."""
    if settings.database_url is None:
        return DbStatus(False, "not_configured", NOT_SET_MESSAGE)
    problem = url_problem(settings.database_url.get_secret_value())
    if problem is not None:
        return DbStatus(False, "misconfigured", problem)
    migration = settings.database_migration_url
    if migration is not None:
        problem = url_problem(migration.get_secret_value(), "DATABASE_MIGRATION_URL")
        if problem is not None:
            return DbStatus(False, "misconfigured", problem)
        if _username(migration) == _username(settings.database_url):
            return DbStatus(False, "misconfigured", SAME_ROLE_MESSAGE)
    return None


def _engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        # Keep bound parameters (memory content, later) out of exception text.
        hide_parameters=True,
        connect_args={"timeout": CONNECT_TIMEOUT_S},
    )


def _refuse_privileged_role(dbapi_connection: Any, _record: Any) -> None:
    """Pool ``connect`` hook: refuse any new connection made as a privileged role."""
    privileged = dbapi_connection.run_async(lambda conn: conn.fetchval(PRIVILEGE_QUERY))
    if privileged is not False:  # True, or None if the role row wasn't found
        raise PrivilegedRoleError(PRIVILEGED_MESSAGE)


def create_engine(settings: Settings) -> AsyncEngine:
    """Core's async engine for ``DATABASE_URL`` (the app role). Does not connect
    yet; each new connection is checked with ``PRIVILEGE_QUERY``."""
    if settings.database_url is None:
        raise DatabaseNotConfigured(NOT_SET_MESSAGE)
    status = url_status(settings)
    if status is not None:
        if status.message == SAME_ROLE_MESSAGE:
            raise PrivilegedRoleError(status.message)
        raise DatabaseConfigError(status.message)
    engine = _engine(settings.database_url.get_secret_value())
    event.listen(engine.sync_engine, "connect", _refuse_privileged_role)
    return engine


def migration_url(settings: Settings) -> tuple[str, SecretStr | None]:
    """The owner URL for Alembic and its setting name: ``DATABASE_MIGRATION_URL``,
    else ``DATABASE_URL`` (a throwaway CI database has only that one)."""
    if settings.database_migration_url is not None:
        return "DATABASE_MIGRATION_URL", settings.database_migration_url
    return "DATABASE_URL", settings.database_url


def create_migration_engine(settings: Settings) -> AsyncEngine:
    """Engine for migrations and owner-only admin steps. No privilege check."""
    name, url = migration_url(settings)
    if url is None:
        raise DatabaseNotConfigured(NOT_SET_MESSAGE)
    problem = url_problem(url.get_secret_value(), name)
    if problem is not None:
        raise DatabaseConfigError(problem)
    return _engine(url.get_secret_value())


@asynccontextmanager
async def engine_for(settings: Settings, engine: AsyncEngine | None) -> AsyncIterator[AsyncEngine]:
    """``engine`` if given; otherwise a new one, disposed on exit. A passed-in engine
    is never disposed: its owner (the app) does that."""
    own_engine = engine is None
    current = engine if engine is not None else create_engine(settings)
    try:
        yield current
    finally:
        if own_engine:
            await current.dispose()


def describe_target(settings: Settings) -> str:
    """``user@host:port/db`` without the password, for messages."""
    if settings.database_url is None:
        return "(not configured)"
    if url_status(settings) is not None:
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
    if any(isinstance(e, PrivilegedRoleError) for e in chain):
        return DbStatus(False, "misconfigured", PRIVILEGED_MESSAGE)
    if names & _AUTH_ERRORS:
        return DbStatus(
            False,
            "auth_failed",
            f"Postgres rejected the login for {target}. For the app role, set its "
            "password from DATABASE_URL with `uv run python -m core.db_admin "
            "set-app-password`. For the owner, the password must match "
            "POSTGRES_PASSWORD in .env; the container sets it only when its data "
            "volume is first created.",
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
    status = url_status(settings)
    if status is not None:
        return status
    target = describe_target(settings)
    async with engine_for(settings, engine) as current:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S + 1):
                async with current.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception as exc:
            return classify(exc, target)
    return DbStatus(True, "ok", f"connected to {target}")


def main() -> int:
    settings = get_settings()
    status = asyncio.run(check_database(settings))
    # Messages are built from safe parts; redact anyway as a second layer.
    print(Redactor(secrets_from(settings))(f"{status.problem}: {status.message}"))
    return 0 if status.ok else 1


if __name__ == "__main__":
    sys.exit(main())
