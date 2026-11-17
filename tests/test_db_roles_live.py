"""The least-privilege app role against the local Postgres (#1, #12). Opt-in.

Core connects as ``aether_app`` (migration 0003); the owner runs migrations only.
These prove what the app role can and can't do, that Core refuses the owner, and
that no password shows up in any message or log. Every statement runs in a
transaction that is rolled back; the throwaway login role (``live_app``) is
dropped after each test.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import importlib.util
import io
import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

import core.db_admin
from core.app import create_app
from core.config import Settings
from core.db import (
    PRIVILEGED_MESSAGE,
    PrivilegedRoleError,
    check_database,
    create_engine,
    create_migration_engine,
)
from core.db_admin import set_app_password
from core.events import EventWriter, route_selected
from core.identity import Identity
from core.log import Redactor, configure_logging
from router.contract import Message, ModelRequest
from router.routing import RouteDecision, RouteOptions, Router
from tests.conftest import APP_ROLE, LIVE_DB
from tests.fakes import FakeProvider
from tests.test_events_live import in_rolled_back_tx, require_events_table

pytestmark = LIVE_DB

REPO = Path(__file__).resolve().parent.parent
INSUFFICIENT_PRIVILEGE = "42501"

# Every one must be refused by the database, not by a trigger or by Core.
FORBIDDEN = [
    "UPDATE events SET outcome = 'ok'",
    "DELETE FROM events",
    "TRUNCATE events",
    "DROP TRIGGER events_no_update_delete ON events",
    "DROP TRIGGER events_no_truncate ON events",
    "ALTER TABLE events DISABLE TRIGGER events_no_update_delete",
    "ALTER TABLE events DISABLE TRIGGER ALL",
    "ALTER TABLE events ADD COLUMN note text",
    "ALTER TABLE events DROP CONSTRAINT events_actor",
    "DROP TABLE events",
    "DROP FUNCTION events_append_only()",
    "CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger"
    " LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    "CREATE TABLE app_role_probe (x int)",
    "CREATE TEMP TABLE app_role_probe (x int)",
    "CREATE SCHEMA app_role_probe",
    "UPDATE alembic_version SET version_num = 'x'",
    "DELETE FROM alembic_version",
    "SET session_replication_role = replica",
]


def password_of(settings: Settings) -> str:
    assert settings.database_url is not None
    return unquote(urlsplit(settings.database_url.get_secret_value()).password or "")


def owner_password(settings: Settings) -> str:
    assert settings.database_migration_url is not None
    return unquote(urlsplit(settings.database_migration_url.get_secret_value()).password or "")


def sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def test_app_role_name_matches_the_migration() -> None:
    path = REPO / "migrations/versions/0003_app_role.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.APP_ROLE == APP_ROLE


def test_app_role_can_insert_and_read_events(live_app: Settings) -> None:
    writer = EventWriter(create_engine(live_app), Redactor())
    request = ModelRequest(messages=(Message(role="user", content="x"),))

    async def body(conn: AsyncConnection) -> tuple[str, str]:
        await require_events_table(conn)
        event = route_selected(RouteDecision("ollama", "local", "test"), request, RouteOptions())
        event_id = await writer.write_with(conn, event)
        row = await conn.execute(
            text("SELECT event_type, current_user FROM events WHERE id = :id"), {"id": event_id}
        )
        event_type, user = row.one()
        return str(event_type), str(user)

    event_type, user = in_rolled_back_tx(live_app, body)
    assert event_type == "model.route.selected"
    assert user.startswith("aether_app_test_")  # really ran as the app role's member


@pytest.mark.parametrize("statement", FORBIDDEN)
def test_app_role_is_refused(live_app: Settings, statement: str) -> None:
    async def body(conn: AsyncConnection) -> str:
        await require_events_table(conn)
        try:
            await conn.execute(text(statement))
        except DBAPIError as exc:
            return sqlstate(exc) or type(exc.orig).__name__
        return "allowed"

    assert in_rolled_back_tx(live_app, body) == INSUFFICIENT_PRIVILEGE


def test_app_role_passes_the_privilege_check(live_app: Settings) -> None:
    status = asyncio.run(check_database(live_app))
    assert status.ok, status.message


def test_nothing_is_granted_to_public(live_owner: Settings) -> None:
    checks = {
        "database": "has_database_privilege('public', current_database(),"
        " 'CONNECT, TEMPORARY, CREATE')",
        "schema": "has_schema_privilege('public', current_schema(), 'USAGE, CREATE')",
        "function": "has_function_privilege('public', 'events_append_only()', 'EXECUTE')",
        "events": "has_table_privilege('public', 'events',"
        " 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')",
        "alembic_version": "has_table_privilege('public', 'alembic_version',"
        " 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')",
    }

    async def body(conn: AsyncConnection) -> dict[str, bool]:
        await require_events_table(conn)
        return {
            name: bool(await conn.scalar(text(f"SELECT {sql}"))) for name, sql in checks.items()
        }

    granted = in_rolled_back_tx(live_owner, body, owner=True)
    assert granted == dict.fromkeys(checks, False)


def test_app_role_has_only_its_attributes(live_owner: Settings) -> None:
    async def body(conn: AsyncConnection) -> tuple[bool, ...]:
        row = await conn.execute(
            text(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls,"
                " rolcanlogin FROM pg_roles WHERE rolname = :r"
            ),
            {"r": APP_ROLE},
        )
        return tuple(row.one())

    assert in_rolled_back_tx(live_owner, body, owner=True) == (
        False,
        False,
        False,
        False,
        False,
        True,
    )


# --- Core refuses the owner ------------------------------------------------------------


def as_core(owner: Settings) -> Settings:
    """The owner URL configured as Core's DATABASE_URL: the mistake to catch."""
    return Settings(_env_file=None, database_url=owner.database_url)


def test_owner_as_core_is_reported_misconfigured(live_owner: Settings) -> None:
    status = asyncio.run(check_database(as_core(live_owner)))
    assert (status.problem, status.message) == ("misconfigured", PRIVILEGED_MESSAGE)
    assert live_owner.database_url is not None
    owner_url = live_owner.database_url.get_secret_value()
    parts = urlsplit(owner_url)
    for part in (owner_url, parts.password, parts.hostname, parts.netloc):
        assert part and part not in status.message


def test_owner_engine_refuses_every_connection(live_owner: Settings) -> None:
    async def connect() -> None:
        engine = create_engine(as_core(live_owner))
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    with pytest.raises(PrivilegedRoleError):
        asyncio.run(connect())


def test_core_refuses_to_start_as_owner(live_owner: Settings, identity: Identity) -> None:
    out = io.StringIO()
    settings = as_core(live_owner)
    configure_logging(settings, stream=out)
    app = create_app(settings, identity, router=_fake_router(identity))
    with pytest.raises(PrivilegedRoleError) as raised, TestClient(app):
        pass  # pragma: no cover - startup must fail
    message = str(raised.value)
    assert message == PRIVILEGED_MESSAGE
    password = password_of(live_owner)
    assert password not in message and password not in out.getvalue()


def test_core_starts_as_app_role(live_app: Settings, identity: Identity) -> None:
    app = create_app(live_app, identity, router=_fake_router(identity))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def _fake_router(identity: Identity) -> Router:
    return Router([FakeProvider("ollama", "local")], identity)


# --- set-app-password (core.db_admin) ----------------------------------------------------


def with_password(settings: Settings, password: str) -> Settings:
    assert settings.database_url is not None
    parts = urlsplit(settings.database_url.get_secret_value())
    netloc = f"{parts.username}:{password}@{parts.hostname}:{parts.port or 5432}"
    return settings.model_copy(
        update={"database_url": SecretStr(urlunsplit(parts._replace(netloc=netloc)))}
    )


def test_set_app_password_sets_it_and_never_echoes_it(
    live_app: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    new_password = "canary-new-app-password-5b1e9d"  # noqa: S105  # gitleaks:allow
    updated = with_password(live_app, new_password)
    out = io.StringIO()
    configure_logging(updated, stream=out)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)  # echo every statement
    monkeypatch.setattr(core.db_admin, "get_settings", lambda: updated)

    code = core.db_admin.main(["set-app-password"])

    printed = capsys.readouterr()
    shown = printed.out + printed.err + out.getvalue()
    assert code == 0, printed.out
    assert printed.out.startswith("ok: password set; Core can log in as aether_app_test_")
    for secret in (new_password, password_of(live_app), owner_password(live_app)):
        assert secret not in shown
    # The new password works; the old one doesn't any more.
    assert asyncio.run(check_database(updated)).ok
    assert asyncio.run(check_database(live_app)).problem == "auth_failed"


def test_set_app_password_needs_the_role(live_app: Settings) -> None:
    assert live_app.database_url is not None
    parts = urlsplit(live_app.database_url.get_secret_value())
    missing = "aether_app_missing_role"
    netloc = f"{missing}:{parts.password}@{parts.hostname}:{parts.port or 5432}"
    settings = live_app.model_copy(
        update={"database_url": SecretStr(urlunsplit(parts._replace(netloc=netloc)))}
    )
    status = asyncio.run(set_app_password(settings))
    assert status.problem == "misconfigured"
    assert "alembic upgrade head" in status.message
    assert password_of(live_app) not in status.message


def test_migration_engine_is_the_owner(live_owner: Settings) -> None:
    async def user() -> str:
        engine = create_migration_engine(live_owner)
        try:
            async with engine.connect() as conn:
                return str(await conn.scalar(text("SELECT current_user")))
        finally:
            await engine.dispose()

    assert asyncio.run(user()) != APP_ROLE


# --- the privilege check catches more than ownership (security review) ---------------


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT pg_write_all_data TO {role}",
        "GRANT pg_write_server_files TO {role}",
        "GRANT pg_execute_server_program TO {role}",
        "GRANT UPDATE ON events TO {role}",
        "GRANT TRIGGER ON events TO {role}",
        "GRANT CREATE ON SCHEMA public TO {role}",
    ],
)
def test_a_wrongly_granted_app_role_is_refused(
    live_app: Settings, live_owner: Settings, grant: str
) -> None:
    assert live_app.database_url is not None
    role = urlsplit(live_app.database_url.get_secret_value()).username
    statement = grant.format(role=role)
    revoke = statement.replace("GRANT", "REVOKE", 1).replace(" TO ", " FROM ")

    async def run(sql: str) -> None:
        engine = create_migration_engine(live_owner)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(sql)
        finally:
            await engine.dispose()

    asyncio.run(run(statement))
    try:
        status = asyncio.run(check_database(live_app))
    finally:
        asyncio.run(run(revoke))  # the fixture drops the role afterwards anyway
    assert (status.problem, status.message) == ("misconfigured", PRIVILEGED_MESSAGE)


def test_set_app_password_refuses_a_role_outside_the_app_role(
    live_app: Settings, live_owner: Settings
) -> None:
    # The owner's own role named in DATABASE_URL (with a different migration user
    # impossible here, so use a fresh unrelated role instead).
    other = "aether_unrelated_probe"

    async def run(sql: str) -> None:
        engine = create_migration_engine(live_owner)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(sql)
        finally:
            await engine.dispose()

    asyncio.run(run(f"CREATE ROLE {other} LOGIN"))
    try:
        assert live_app.database_url is not None
        parts = urlsplit(live_app.database_url.get_secret_value())
        netloc = f"{other}:{parts.password}@{parts.hostname}:{parts.port or 5432}"
        settings = live_app.model_copy(
            update={"database_url": SecretStr(urlunsplit(parts._replace(netloc=netloc)))}
        )
        status = asyncio.run(set_app_password(settings))
        can_login = asyncio.run(check_database(settings)).problem
    finally:
        asyncio.run(run(f"DROP ROLE IF EXISTS {other}"))
    assert status.problem == "misconfigured"
    assert "was not changed" in status.message
    assert can_login == "auth_failed"  # no password was set on it
