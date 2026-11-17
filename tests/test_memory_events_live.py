"""Migration 0005 (memory events, ADR-024 §5, #72) against a live Postgres. Opt-in.

Nothing persists: the CHECK and grant tests run in transactions that are rolled
back, and the migration round trip runs in a throwaway schema inside a rolled-back
transaction, so the real ``public.events`` is never altered. Inserts run as Core
does, through the app role (``live_app``); the round trip runs as the owner.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import uuid
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.config import Settings
from tests.conftest import LIVE_DB
from tests.test_events_live import REPO, SNAPSHOT, in_rolled_back_tx, require_events_table

pytestmark = LIVE_DB

ID_A, ID_B = str(uuid.uuid4()), str(uuid.uuid4())

RECORD: dict[str, Any] = {
    "event_type": "memory.deleted",
    "actor": "memory",
    "memory_record_id": ID_A,
    "memory_category": "preference",
    "memory_source": "owner",
    "memory_level": "personal",
}
RETRIEVED: dict[str, Any] = {
    "event_type": "memory.context.retrieved",
    "actor": "memory",
    "request_id": "req-live-1",
    "provider": "ollama",
    "memory_count_public": 1,
    "memory_count_personal": 1,
    "memory_count_sensitive": 0,
    "memory_chars": 120,
    "memory_effective_level": "personal",
    "memory_record_ids": [ID_A, ID_B],
    "memory_left_out": 3,
}
EXPORTED: dict[str, Any] = {
    "event_type": "memory.exported",
    "actor": "memory",
    "memory_export_count": 12,
}
BASE: dict[str, Any] = {"event_type": "request.received", "actor": "core", "privacy": "personal"}
CASTS = {"memory_record_id": "uuid", "memory_record_ids": "uuid[]"}


def insert_sql(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    columns = ["occurred_at", "schema_version", *row]
    values = ["now()", "1"]
    params: dict[str, Any] = {}
    for name, value in row.items():
        params[name] = value
        values.append(f"CAST(:{name} AS {CASTS[name]})" if name in CASTS else f":{name}")
    sql = f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join(values)}) RETURNING id"  # noqa: S608
    return sql, params


async def try_insert(conn: AsyncConnection, row: dict[str, Any]) -> str:
    sql, params = insert_sql(row)
    nested = await conn.begin_nested()
    try:
        await conn.execute(text(sql), params)
    except DBAPIError as exc:
        await nested.rollback()
        return f"{type(exc.orig).__name__} {exc.orig}"
    await nested.commit()
    return "accepted"


def test_app_role_inserts_memory_events_but_cannot_change_them(live_app: Settings) -> None:
    async def body(conn: AsyncConnection) -> list[str]:
        await require_events_table(conn)
        results = [await try_insert(conn, row) for row in (RECORD, RETRIEVED, EXPORTED)]
        for kind in ("memory.proposed", "memory.approved", "memory.rejected", "memory.expired"):
            results.append(await try_insert(conn, {**RECORD, "event_type": kind}))
        results.append(
            await try_insert(
                conn, {**RECORD, "event_type": "memory.updated", "memory_old_level": "public"}
            )
        )
        results.append(await try_insert(conn, {**RECORD, "actor": "restore"}))
        for statement in (
            "UPDATE events SET memory_level = 'public' WHERE event_type LIKE 'memory.%'",
            "DELETE FROM events WHERE event_type LIKE 'memory.%'",
        ):
            nested = await conn.begin_nested()
            try:
                await conn.execute(text(statement))
                results.append("changed")
            except DBAPIError as exc:
                results.append(str(exc.orig))
            await nested.rollback()
        return results

    results = in_rolled_back_tx(live_app, body)
    assert results[:-2] == ["accepted"] * 9
    assert all("permission denied for table events" in r for r in results[-2:]), results[-2:]


def _uuids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


BAD_ROWS: dict[str, dict[str, Any]] = {
    # missing a required column
    "record without id": {**RECORD, "memory_record_id": None},
    "record without category": {**RECORD, "memory_category": None},
    "record without source": {**RECORD, "memory_source": None},
    "record without level": {**RECORD, "memory_level": None},
    "retrieved without request id": {**RETRIEVED, "request_id": None},
    "retrieved without provider": {**RETRIEVED, "provider": None},
    "retrieved without counts": {**RETRIEVED, "memory_count_sensitive": None},
    "retrieved without chars": {**RETRIEVED, "memory_chars": None},
    "retrieved without effective level": {**RETRIEVED, "memory_effective_level": None},
    "retrieved without ids": {**RETRIEVED, "memory_record_ids": None},
    "retrieved without left out": {**RETRIEVED, "memory_left_out": None},
    "exported without count": {**EXPORTED, "memory_export_count": None},
    # a column set on the wrong event type
    "record id on request.received": {**BASE, "memory_record_id": ID_A},
    "category on request.received": {**BASE, "memory_category": "task"},
    "level on model.call.completed": {
        "event_type": "model.call.completed",
        "actor": "router",
        "memory_level": "public",
    },
    "ids on request.failed": {
        "event_type": "request.failed",
        "actor": "router",
        "memory_record_ids": [ID_A],
    },
    "export count on request.received": {**BASE, "memory_export_count": 1},
    "count on a record event": {**RECORD, "memory_count_public": 0},
    "chars on exported": {**EXPORTED, "memory_chars": 5},
    "record id on retrieved": {**RETRIEVED, "memory_record_id": ID_A},
    "export count on retrieved": {**RETRIEVED, "memory_export_count": 2},
    "record fields on exported": {**EXPORTED, "memory_record_id": ID_A},
    "old level on deleted": {**RECORD, "memory_old_level": "public"},
    "old level equal to new": {
        **RECORD,
        "event_type": "memory.updated",
        "memory_old_level": "personal",
    },
    # bad values
    "secret level": {**RECORD, "memory_level": "secret"},
    "secret old level": {**RECORD, "event_type": "memory.updated", "memory_old_level": "secret"},
    "secret effective level": {**RETRIEVED, "memory_effective_level": "secret"},
    "unknown level": {**RECORD, "memory_level": "private"},
    "unknown category": {**RECORD, "memory_category": "operational"},
    "unknown source": {**RECORD, "memory_source": "user"},
    "unknown memory type": {**RECORD, "event_type": "memory.read"},
    "negative count": {**RETRIEVED, "memory_count_public": -1, "memory_count_personal": 3},
    "negative chars": {**RETRIEVED, "memory_chars": -1},
    "negative left out": {**RETRIEVED, "memory_left_out": -1},
    "negative export count": {**EXPORTED, "memory_export_count": -1},
    "21 ids": {
        **RETRIEVED,
        "memory_count_public": 21,
        "memory_count_personal": 0,
        "memory_record_ids": _uuids(21),
    },
    "null id in list": {**RETRIEVED, "memory_record_ids": [ID_A, None]},
    "ids do not match counts": {**RETRIEVED, "memory_record_ids": [ID_A]},
    "effective below a level sent": {**RETRIEVED, "memory_effective_level": "public"},
    # wrong actor
    "memory event as core": {**RECORD, "actor": "core"},
    "memory event as router": {**RETRIEVED, "actor": "router"},
    "request.received as memory": {**BASE, "actor": "memory"},
    "request.failed as restore": {"event_type": "request.failed", "actor": "restore"},
    "unknown actor": {**RECORD, "actor": "model"},
}


@pytest.mark.parametrize("name", sorted(BAD_ROWS))
def test_live_checks_refuse_bad_memory_events(live_app: Settings, name: str) -> None:
    async def body(conn: AsyncConnection) -> str:
        await require_events_table(conn)
        return await try_insert(conn, BAD_ROWS[name])

    result = in_rolled_back_tx(live_app, body)
    assert "CheckViolation" in result, result


def test_live_twenty_ids_and_empty_retrieval_are_accepted(live_app: Settings) -> None:
    async def body(conn: AsyncConnection) -> list[str]:
        await require_events_table(conn)
        twenty = {
            **RETRIEVED,
            "memory_count_public": 20,
            "memory_count_personal": 0,
            "memory_effective_level": "public",
            "memory_record_ids": _uuids(20),
        }
        empty = {
            **RETRIEVED,
            "memory_count_public": 0,
            "memory_count_personal": 0,
            "memory_chars": 0,
            "memory_record_ids": [],
        }
        return [await try_insert(conn, twenty), await try_insert(conn, empty)]

    assert in_rolled_back_tx(live_app, body) == ["accepted", "accepted"]


# --- the round trip: 0004 -> 0005 -> 0004 in a throwaway schema --------------------------

OLD_ROWS = [
    "INSERT INTO events (occurred_at, event_type, schema_version, actor, privacy, allow_cloud,"
    " request_id, session_id) VALUES (now(), 'request.received', 1, 'core', 'personal', false,"
    " 'req-old-1', 'sess-1')",
    "INSERT INTO events (occurred_at, event_type, schema_version, actor, privacy, provider,"
    " provider_kind, route_reason) VALUES (now(), 'model.route.selected', 1, 'router', 'public',"
    " 'ollama', 'local', 'local first')",
    "INSERT INTO events (occurred_at, event_type, schema_version, actor, outcome, provider,"
    " model, finish_reason, latency_ms, input_tokens, output_tokens) VALUES (now(),"
    " 'model.call.completed', 1, 'router', 'ok', 'claude', 'claude-x', 'stop', 12.5, 3, 4)",
    "INSERT INTO events (occurred_at, event_type, schema_version, actor, outcome, provider,"
    " error_type, error_message) VALUES (now(), 'provider.unavailable', 1, 'router', 'error',"
    " 'ollama', 'ProviderUnavailable', 'down')",
    "INSERT INTO events (occurred_at, event_type, schema_version, actor, outcome, error_type,"
    " attempts) VALUES (now(), 'request.failed', 1, 'router', 'error', 'RouteFailed',"
    ' \'[{"provider": "ollama", "error_type": "X", "message": "m", "retryable": true}]\')',
]
CONSTRAINTS = """
SELECT con.conname || ' ' || pg_get_constraintdef(con.oid) FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = :schema AND c.relname = 'events'
"""
COLUMN_LIST = """
SELECT a.attname || ' ' || format_type(a.atttypid, a.atttypmod) || ' ' || a.attnotnull::text
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = :schema AND c.relname = 'events' AND a.attnum > 0 AND NOT a.attisdropped
"""
ROWS = "SELECT id::text || ':' || xmin::text FROM events"
APP_RIGHTS = (
    "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES',"
    "'TRIGGER']) p WHERE has_table_privilege('aether_app', 'events', p)"
)
TRIGGERS = (
    "SELECT tgname || ':' || tgenabled::text FROM pg_trigger"
    " WHERE tgrelid = 'events'::regclass AND NOT tgisinternal"
)


def test_live_migration_round_trip_0004_0005(live_owner: Settings) -> None:
    schema = f"aether_migtest_{uuid.uuid4().hex[:12]}"
    config = Config(str(REPO / "alembic.ini"))

    def q(sync: Connection, sql: str) -> set[str]:
        return {r[0] for r in sync.execute(text(sql), {"schema": schema})}

    def state(sync: Connection) -> dict[str, set[str]]:
        return {
            "objects": q(sync, SNAPSHOT),
            "constraints": q(sync, CONSTRAINTS),
            "columns": q(sync, COLUMN_LIST),
            "rows": q(sync, ROWS),
            "app": q(sync, APP_RIGHTS),
            "triggers": q(sync, TRIGGERS),
        }

    def migrate(sync: Connection) -> dict[str, Any]:
        public_before = q(sync, SNAPSHOT.replace(":schema", "'public'"))
        sync.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config.attributes["connection"] = sync
        command.upgrade(config, "0004")
        for statement in OLD_ROWS:
            sync.execute(text(statement))
        at_0004 = state(sync)
        command.upgrade(config, "0005")  # validates every new CHECK against the old rows
        at_0005 = state(sync)

        # A memory event makes the downgrade refuse, and the refusal changes nothing.
        savepoint = sync.begin_nested()
        sql, params = insert_sql(RECORD)
        sync.execute(text(sql), params)
        try:
            command.downgrade(config, "0004")
            refused = "no error"
        except DBAPIError as exc:
            refused = str(exc.orig)
        savepoint.rollback()  # also removes the memory event (rolled back, never deleted)
        after_refusal = state(sync)

        command.downgrade(config, "0004")  # no memory events: allowed
        back_at_0004 = state(sync)
        return {
            "public_before": public_before,
            "public_after": q(sync, SNAPSHOT.replace(":schema", "'public'")),
            "at_0004": at_0004,
            "at_0005": at_0005,
            "refused": refused,
            "after_refusal": after_refusal,
            "back_at_0004": back_at_0004,
        }

    async def body(conn: AsyncConnection) -> dict[str, Any]:
        result: dict[str, Any] = await conn.run_sync(migrate)
        return result

    s = in_rolled_back_tx(live_owner, body, owner=True)
    at_0004, at_0005 = s["at_0004"], s["at_0005"]

    # Existing rows: still there, same row versions (not rewritten), all valid.
    assert len(at_0004["rows"]) == len(OLD_ROWS)
    assert at_0005["rows"] == at_0004["rows"]
    # No table, index, function or trigger added or removed; triggers still enabled.
    assert at_0005["objects"] == at_0004["objects"]
    assert (
        at_0005["triggers"]
        == at_0004["triggers"]
        == {
            "events_no_update_delete:O",
            "events_no_truncate:O",
        }
    )
    # App rights unchanged: INSERT and SELECT on events, nothing more.
    assert at_0005["app"] == at_0004["app"] == {"SELECT", "INSERT"}
    # Only nullable memory_* columns added.
    added_columns = at_0005["columns"] - at_0004["columns"]
    assert at_0004["columns"] <= at_0005["columns"]
    assert all(c.startswith("memory_") and c.endswith(" false") for c in added_columns)
    assert len(added_columns) == 13
    assert "memory_record_ids uuid[] false" in added_columns
    # Constraints: the two widened, the new ones added, every other one unchanged.
    removed = {c.split()[0] for c in at_0004["constraints"] - at_0005["constraints"]}
    assert removed == {"events_event_type", "events_actor"}
    added = {c.split()[0] for c in at_0005["constraints"] - at_0004["constraints"]}
    assert {"events_event_type", "events_actor"} < added
    assert all(name.startswith("events_") for name in added)

    assert "downgrade refused" in s["refused"] and "permanent" in s["refused"]
    assert s["after_refusal"] == at_0005
    assert s["back_at_0004"] == at_0004  # downgrade restores exactly the 0004 state
    assert s["public_after"] == s["public_before"]  # the real schema is untouched
