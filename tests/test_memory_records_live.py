"""The ``memory_records`` table against a live Postgres (#66, ADR-024 §1). Opt-in.

Proves that every CHECK refuses a bad row, what the app role can and can't do
with the table, that Core's privilege check allows UPDATE and DELETE on this
table only, and that the migration round trip (0003 -> 0004 -> 0003) leaves
``events`` untouched. Every statement runs in a transaction that is rolled back;
the migration round trip runs in a throwaway schema. All content is invented
canary text.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.config import Settings
from core.db import PRIVILEGED_MESSAGE, check_database, create_migration_engine
from tests.conftest import LIVE_DB
from tests.test_events_live import _BASE, SNAPSHOT, in_rolled_back_tx

pytestmark = LIVE_DB

REPO = Path(__file__).resolve().parent.parent
INSUFFICIENT_PRIVILEGE = "42501"
CHECK_VIOLATION = "23514"
NOT_NULL_VIOLATION = "23502"
UNIQUE_VIOLATION = "23505"
CANARY = "canary-7f3e: the owner's invented favourite colour is teal"

# A valid row; each bad case below overrides one or two columns.
GOOD: dict[str, Any] = {
    "content": CANARY,
    "key": "test.favourite_colour",
    "category": "preference",
    "privacy": "personal",
    "status": "approved",
    "source": "owner",
    "source_provider": None,
    "source_request_id": None,
    "privacy_source": "owner",
    "approved_at": "now",
}
INSERT = (
    "INSERT INTO memory_records (content, key, category, privacy, status, source,"
    " source_provider, source_request_id, privacy_source, approved_at) VALUES (:content,"
    " :key, :category, :privacy, :status, :source, :source_provider, :source_request_id,"
    " :privacy_source, CAST(:approved_at AS timestamptz)) RETURNING id"
)


def row(**changes: Any) -> dict[str, Any]:
    values = {**GOOD, **changes}
    if values["approved_at"] == "now":
        values["approved_at"] = datetime(2026, 11, 16, 12, tzinfo=UTC)
    return values


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


async def require_memory_table(conn: AsyncConnection) -> None:
    exists = await conn.scalar(text("SELECT to_regclass('public.memory_records') IS NOT NULL"))
    if not exists:
        pytest.fail("memory_records missing: run `uv run alembic upgrade head`")


async def try_insert(conn: AsyncConnection, *rows: dict[str, Any]) -> str:
    """Insert ``rows`` in a savepoint. "ok", or the first refusal as its SQLSTATE plus
    the name of the constraint that refused it (so each case proves its own CHECK)."""
    savepoint = await conn.begin_nested()
    try:
        for values in rows:
            await conn.execute(text(INSERT), values)
    except DBAPIError as exc:
        await savepoint.rollback()
        # SQLAlchemy wraps asyncpg's error; the asyncpg one carries the name.
        cause = getattr(exc.orig, "__cause__", None)
        name = getattr(cause, "constraint_name", None) or getattr(exc.orig, "constraint_name", None)
        code = sqlstate(exc) or type(exc.orig).__name__
        return f"{code}:{name}" if name else code
    await savepoint.rollback()
    return "ok"


# --- every CHECK refuses a bad row ---------------------------------------------------


def check(name: str) -> str:
    return f"{CHECK_VIOLATION}:memory_records_{name}"


# Each case breaks exactly one constraint, which the expected value names.
PENDING: dict[str, Any] = {"status": "pending", "approved_at": None, "privacy_source": "suggested"}
BAD_ROWS: list[tuple[str, dict[str, Any], str]] = [
    ("secret level", {"privacy": "secret"}, check("privacy")),
    ("unknown level", {"privacy": "Secret"}, check("privacy")),
    ("no level", {"privacy": None}, NOT_NULL_VIOLATION),
    ("content too long", {"content": "x" * 1001}, check("content")),
    ("empty content", {"content": ""}, check("content")),
    ("no content", {"content": None}, NOT_NULL_VIOLATION),
    ("key with uppercase", {"key": "Favourite"}, check("key")),
    ("key with a space", {"key": "favourite colour"}, check("key")),
    ("key with a dash", {"key": "favourite-colour"}, check("key")),
    ("empty key", {"key": ""}, check("key")),
    ("key too long", {"key": "k" * 65}, check("key")),
    ("bad category", {"category": "operational"}, check("category")),
    ("no category", {"category": None}, NOT_NULL_VIOLATION),
    ("bad status", {"status": "rejected"}, check("status")),
    ("bad source", {"source": "web"}, check("source")),
    ("bad privacy_source", {**PENDING, "privacy_source": "model"}, check("privacy_source")),
    ("bad provider", {"source_provider": "Claude API"}, check("source_provider")),
    ("bad request id", {"source_request_id": "a b"}, check("source_request_id")),
    ("approved, level not chosen by the owner", {"privacy_source": "suggested"}, check("approved")),
    ("approved without approved_at", {"approved_at": None}, check("approved")),
    ("pending with approved_at", {**PENDING, "approved_at": "now"}, check("pending")),
]


@pytest.mark.parametrize(
    ("changes", "expected"), [(c, e) for _, c, e in BAD_ROWS], ids=[n for n, _, _ in BAD_ROWS]
)
def test_every_check_refuses_a_bad_row(
    live_app: Settings, changes: dict[str, Any], expected: str
) -> None:
    async def body(conn: AsyncConnection) -> tuple[str, str]:
        await require_memory_table(conn)
        return await try_insert(conn, row()), await try_insert(conn, row(**changes))

    assert in_rolled_back_tx(live_app, body) == ("ok", expected)


def test_boundary_rows_are_accepted(live_app: Settings) -> None:
    good = [
        row(content="x" * 1000, key="k" * 64),
        row(key=None),
        row(key="a.b_c9", category="reference", privacy="sensitive"),
        row(
            status="pending", approved_at=None, privacy_source="suggested", source="model",
            source_provider="ollama", source_request_id="req-0123_abc.9", privacy="public",
        ),
        row(status="pending", approved_at=None, source="tool", privacy_source="suggested"),
    ]  # fmt: skip

    async def body(conn: AsyncConnection) -> list[str]:
        await require_memory_table(conn)
        return [await try_insert(conn, values) for values in good]

    assert in_rolled_back_tx(live_app, body) == ["ok"] * len(good)


def test_at_most_one_approved_record_per_key(live_app: Settings) -> None:
    pending = row(**PENDING)

    async def body(conn: AsyncConnection) -> dict[str, str]:
        await require_memory_table(conn)
        return {
            "two approved, one key": await try_insert(conn, row(), row()),
            "approved + pending": await try_insert(conn, row(), pending),
            "two pending": await try_insert(conn, pending, pending),
            "two approved, no key": await try_insert(conn, row(key=None), row(key=None)),
            "two approved, two keys": await try_insert(conn, row(), row(key="test.other")),
        }

    assert in_rolled_back_tx(live_app, body) == {
        "two approved, one key": f"{UNIQUE_VIOLATION}:memory_records_approved_key",
        "approved + pending": "ok",
        "two pending": "ok",
        "two approved, no key": "ok",
        "two approved, two keys": "ok",
    }


# --- what the app role can and can't do ---------------------------------------------


def test_app_role_can_insert_read_update_and_delete(live_app: Settings) -> None:
    async def body(conn: AsyncConnection) -> list[Any]:
        await require_memory_table(conn)
        record_id = (await conn.execute(text(INSERT), row())).scalar_one()
        read = await conn.scalar(
            text("SELECT content FROM memory_records WHERE id = :id"), {"id": record_id}
        )
        updated = await conn.execute(
            text(
                "UPDATE memory_records SET privacy = 'sensitive', updated_at = now() WHERE id = :id"
            ),
            {"id": record_id},
        )
        deleted = await conn.execute(
            text("DELETE FROM memory_records WHERE id = :id"), {"id": record_id}
        )
        left = await conn.scalar(
            text("SELECT count(*) FROM memory_records WHERE id = :id"), {"id": record_id}
        )
        user = await conn.scalar(text("SELECT current_user"))
        return [read, updated.rowcount, deleted.rowcount, left, str(user)]

    read, updated, deleted, left, user = in_rolled_back_tx(live_app, body)
    assert (read, updated, deleted, left) == (CANARY, 1, 1, 0)
    assert user.startswith("aether_app_test_")  # really ran as the app role's member


FORBIDDEN = [
    "TRUNCATE memory_records",
    "CREATE TRIGGER memory_probe BEFORE INSERT ON memory_records"
    " FOR EACH ROW EXECUTE FUNCTION events_append_only()",
    "ALTER TABLE memory_records DISABLE TRIGGER ALL",
    "ALTER TABLE memory_records ADD COLUMN note text",
    "ALTER TABLE memory_records DROP CONSTRAINT memory_records_privacy",
    "ALTER TABLE memory_records ALTER COLUMN privacy DROP NOT NULL",
    "DROP INDEX memory_records_approved_key",
    "DROP TABLE memory_records",
    "CREATE TABLE memory_probe (id uuid REFERENCES memory_records (id))",
]


@pytest.mark.parametrize("statement", FORBIDDEN)
def test_app_role_cannot_truncate_add_triggers_alter_or_drop(
    live_app: Settings, statement: str
) -> None:
    async def body(conn: AsyncConnection) -> str:
        await require_memory_table(conn)
        try:
            await conn.execute(text(statement))
        except DBAPIError as exc:
            return sqlstate(exc) or type(exc.orig).__name__
        return "allowed"

    assert in_rolled_back_tx(live_app, body) == INSUFFICIENT_PRIVILEGE


def test_grants_are_exactly_the_four(live_owner: Settings) -> None:
    async def body(conn: AsyncConnection) -> dict[str, list[str]]:
        await require_memory_table(conn)
        rows = await conn.execute(
            text(
                "SELECT coalesce(r.rolname, 'PUBLIC'), a.privilege_type"
                " FROM pg_class c, aclexplode(c.relacl) a"
                " LEFT JOIN pg_roles r ON r.oid = a.grantee"
                " WHERE c.oid = 'memory_records'::regclass AND a.grantee <> c.relowner"
                " ORDER BY 1, 2"
            )
        )
        found: dict[str, list[str]] = {}
        for grantee, privilege in rows:
            found.setdefault(str(grantee), []).append(str(privilege))
        return found

    assert in_rolled_back_tx(live_owner, body, owner=True) == {
        "aether_app": ["DELETE", "INSERT", "SELECT", "UPDATE"]
    }


# --- Core's privilege check: UPDATE and DELETE allowed on memory_records only -------


def test_the_app_role_with_the_0004_grants_is_accepted(live_app: Settings) -> None:
    async def body(conn: AsyncConnection) -> list[bool]:
        await require_memory_table(conn)
        return [
            bool(await conn.scalar(text(f"SELECT has_table_privilege('memory_records', '{p}')")))
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
        ]

    assert in_rolled_back_tx(live_app, body) == [True] * 4  # it really holds them...
    status = asyncio.run(check_database(live_app))
    assert status.ok, status.message  # ...and Core still accepts it


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT UPDATE ON events TO {role}",
        "GRANT DELETE ON events TO {role}",
        "GRANT UPDATE (outcome) ON events TO {role}",
        "GRANT UPDATE ON alembic_version TO {role}",
        "GRANT DELETE ON alembic_version TO {role}",
        "GRANT TRUNCATE ON memory_records TO {role}",
        "GRANT TRIGGER ON memory_records TO {role}",
        "GRANT TRUNCATE ON events TO {role}",
    ],
)
def test_a_role_with_more_than_the_0004_grants_is_refused(
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


def test_update_on_another_table_named_like_memory_is_refused(
    live_app: Settings, live_owner: Settings
) -> None:
    # The exception is the exact name memory_records, not a prefix or pattern.
    assert live_app.database_url is not None
    role = urlsplit(live_app.database_url.get_secret_value()).username
    table = f"memory_records_probe_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        engine = create_migration_engine(live_owner)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(sql)
        finally:
            await engine.dispose()

    asyncio.run(run(f"CREATE TABLE {table} (x int)"))
    try:
        asyncio.run(run(f"GRANT UPDATE ON {table} TO {role}"))
        status = asyncio.run(check_database(live_app))
    finally:
        asyncio.run(run(f"DROP TABLE {table}"))
    assert (status.problem, status.message) == ("misconfigured", PRIVILEGED_MESSAGE)


def test_memory_records_in_another_schema_is_not_exempt(
    live_app: Settings, live_owner: Settings
) -> None:
    # Only public.memory_records is exempt: a same-named table elsewhere (which only
    # the owner could create) with UPDATE or DELETE for the app role is refused.
    assert live_app.database_url is not None
    role = urlsplit(live_app.database_url.get_secret_value()).username
    schema = f"aether_probe_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        engine = create_migration_engine(live_owner)
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(sql)
        finally:
            await engine.dispose()

    results = {}
    asyncio.run(run(f"CREATE SCHEMA {schema}"))
    try:
        asyncio.run(run(f"CREATE TABLE {schema}.memory_records (x int)"))
        for right in ("UPDATE", "DELETE"):
            asyncio.run(run(f"GRANT {right} ON {schema}.memory_records TO {role}"))
            results[right] = asyncio.run(check_database(live_app)).problem
            asyncio.run(run(f"REVOKE {right} ON {schema}.memory_records FROM {role}"))
        results["none"] = asyncio.run(check_database(live_app)).problem
    finally:
        asyncio.run(run(f"DROP SCHEMA {schema} CASCADE"))
    assert results == {"UPDATE": "misconfigured", "DELETE": "misconfigured", "none": "ok"}


# --- migration round trip: 0003 -> 0004 -> 0003 leaves events untouched -------------


def test_live_migration_round_trip_0003_0004(live_owner: Settings) -> None:
    schema = f"aether_migtest_{uuid.uuid4().hex[:12]}"
    config = Config(str(REPO / "alembic.ini"))
    # Same id and same xmin: the event row is the same row, not a rewritten copy.
    row_sql = "SELECT id::text || ':' || xmin::text FROM events"
    app_can = (
        "SELECT p FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p"
        " WHERE has_table_privilege('aether_app', 'events', p)"
    )

    def snapshot(sync: Connection) -> set[str]:
        return {r[0] for r in sync.execute(text(SNAPSHOT), {"schema": schema})}

    def rows(sync: Connection, sql: str) -> set[str]:
        return {r[0] for r in sync.execute(text(sql))}

    def migrate(sync: Connection) -> dict[str, set[str]]:
        sync.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config.attributes["connection"] = sync
        command.upgrade(config, "0003")
        sync.execute(text(_BASE + ") VALUES (now(), 'request.received', 1, 'core')"))
        result = {"at_0003": snapshot(sync), "row_0003": rows(sync, row_sql)}
        result["app_0003"] = rows(sync, app_can)
        command.upgrade(config, "0004")
        result |= {"at_0004": snapshot(sync), "row_0004": rows(sync, row_sql)}
        result["app_0004"] = rows(sync, app_can)
        command.downgrade(config, "0003")
        result |= {"back_0003": snapshot(sync), "row_back": rows(sync, row_sql)}
        result["app_back"] = rows(sync, app_can)
        result["version"] = rows(sync, "SELECT version_num FROM alembic_version")
        return result

    async def body(conn: AsyncConnection) -> dict[str, set[str]]:
        result: dict[str, set[str]] = await conn.run_sync(migrate)
        return result

    snaps = in_rolled_back_tx(live_owner, body, owner=True)

    assert snaps["at_0004"] - snaps["at_0003"] == {
        "rel:memory_records:r",
        "rel:memory_records_pkey:i",
        "rel:memory_records_approved_key:i",
    }
    assert snaps["at_0003"] <= snaps["at_0004"]  # nothing from 0003 removed
    assert snaps["back_0003"] == snaps["at_0003"]  # downgrade drops exactly that
    assert len(snaps["row_0003"]) == 1
    assert snaps["row_0003"] == snaps["row_0004"] == snaps["row_back"]  # events untouched
    assert snaps["app_0003"] == snaps["app_0004"] == snaps["app_back"] == {"SELECT", "INSERT"}
    assert snaps["version"] == {"0003"}
