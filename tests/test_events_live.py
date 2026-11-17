"""Events against the local Postgres (#21). Opt-in, and leaves no trace.

Every test runs inside a transaction that is rolled back: rows are inserted,
read back and then undone. The migration round trip runs in a throwaway schema
inside a rolled-back transaction, so the real ``public.events`` is never
dropped or altered. Inserts run as Core does, through the app role
(``live_app``); the trigger and migration tests run as the owner (``live_owner``),
since the app role can't reach them (tests/test_db_roles_live.py).

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from core.config import Settings
from core.db import create_engine, create_migration_engine
from core.events import EventWriter, provider_unavailable, request_failed, route_selected
from core.log import Redactor
from router.contract import Message, ModelRequest, ProviderUnavailable
from router.routing import Attempt, RouteDecision, RouteFailed, RouteOptions
from tests.conftest import LIVE_DB

pytestmark = LIVE_DB

REPO = Path(__file__).resolve().parent.parent
FAKE_SECRET = "fake-configured-secret-live-4e8a"  # noqa: S105  # gitleaks:allow
FAKE_KEY = "sk-ant-api03-FAKE-live-unconfigured-abcdef"  # gitleaks:allow
PROMPT = "PRIVATE-PROMPT-MARKER live: my bank PIN is 4711"


def in_rolled_back_tx(
    settings: Settings,
    body: Callable[[AsyncConnection], Awaitable[Any]],
    *,
    owner: bool = False,
) -> Any:
    """Run ``body`` in a transaction that is always rolled back. ``owner`` connects
    with the migration engine (no privilege check) instead of Core's engine."""

    async def run() -> Any:
        engine = create_migration_engine(settings) if owner else create_engine(settings)
        try:
            async with engine.connect() as conn:
                tx = await conn.begin()
                try:
                    return await body(conn)
                finally:
                    await tx.rollback()  # always: nothing persists
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def require_events_table(conn: AsyncConnection) -> None:
    exists = await conn.scalar(text("SELECT to_regclass('public.events') IS NOT NULL"))
    if not exists:
        pytest.fail("events table missing: run `uv run alembic upgrade head`")


def error_with_secret_cause() -> ProviderUnavailable:
    try:
        try:
            raise RuntimeError(f"upstream said {FAKE_KEY} {FAKE_SECRET} for {PROMPT}")
        except RuntimeError as inner:
            raise ProviderUnavailable("claude", "cannot connect to the Claude API") from inner
    except ProviderUnavailable as exc:
        return exc


def test_live_events_never_store_secret_prompt_or_traceback(live_app: Settings) -> None:
    request = ModelRequest(messages=(Message(role="user", content=PROMPT),))
    writer = EventWriter(create_engine(live_app), Redactor([FAKE_SECRET]))
    exc = error_with_secret_cause()
    echo = Attempt("ollama", "ProviderBadResponse", f"echo {PROMPT} {FAKE_SECRET}", False)
    leaky_reason = f"fallback failed near {FAKE_KEY}"
    options = RouteOptions(allow_cloud=True)

    async def body(conn: AsyncConnection) -> list[dict[str, Any]]:
        await require_events_table(conn)
        events = [
            route_selected(
                RouteDecision("claude", "cloud", "cloud: local unavailable"), request, options
            ),
            provider_unavailable(Attempt.from_error(exc), request),
            provider_unavailable(echo, request),
            request_failed(
                RouteFailed(leaky_reason, [Attempt.from_error(exc), echo]), request, options
            ),
        ]
        ids = [await writer.write_with(conn, e, request=request) for e in events]
        rows = await conn.execute(
            text("SELECT row_to_json(e)::text AS j FROM events e WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": ids},
        )
        import json

        return [json.loads(r.j) for r in rows]

    rows = in_rolled_back_tx(live_app, body)

    assert len(rows) == 4
    everything = repr(rows)
    for forbidden in (FAKE_SECRET, FAKE_KEY, PROMPT, "Traceback", "upstream said"):
        assert forbidden not in everything, forbidden
    assert rows[1]["error_type"] == "ProviderUnavailable"
    assert rows[1]["error_message"] == "cannot connect to the Claude API"
    assert rows[2]["error_message"] == "[REMOVED: contained request text]"
    assert rows[3]["route_reason"] == "fallback failed near [REDACTED]"
    assert [a["provider"] for a in rows[3]["attempts"]] == ["claude", "ollama"]
    assert rows[0]["recorded_at"] is not None and rows[0]["occurred_at"] is not None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET outcome = 'ok' WHERE id = :id",
        "DELETE FROM events WHERE id = :id",
        "TRUNCATE events",
    ],
)
def test_live_events_are_append_only(live_owner: Settings, statement: str) -> None:
    # As the owner: the app role is refused before the trigger runs, so only the
    # owner reaches it. The trigger is the second layer behind the grants.
    writer = EventWriter(create_migration_engine(live_owner), Redactor())
    request = ModelRequest(messages=(Message(role="user", content="x"),))

    async def body(conn: AsyncConnection) -> str:
        await require_events_table(conn)
        event_id = await writer.write_with(
            conn, route_selected(RouteDecision("ollama", "local", "test"), request, RouteOptions())
        )
        nested = await conn.begin_nested()
        try:
            await conn.execute(text(statement), {"id": event_id})
        except DBAPIError as exc:
            await nested.rollback()
            return str(exc.orig)
        await nested.rollback()
        return "no error"

    message = in_rolled_back_tx(live_owner, body, owner=True)
    assert "events is append-only" in message


_BASE = "INSERT INTO events (occurred_at, event_type, schema_version, actor"
BAD_INSERTS = {
    "request_id": _BASE + ", request_id) VALUES (now(), 'request.failed', 1, 'router', :v)",
    "provider": _BASE + ", provider) VALUES (now(), 'request.failed', 1, 'router', :v)",
    "error_message": _BASE + ", error_message) VALUES (now(), 'request.failed', 1, 'router', :v)",
    "attempts": _BASE
    + ", attempts) VALUES (now(), 'request.failed', 1, 'router', CAST(:v AS jsonb))",
    "event_type": _BASE + ") VALUES (now(), :v, 1, 'router')",
}
BAD_VALUES = {
    "request_id": "has space",
    "provider": "Claude!",
    "error_message": "x" * 501,
    "attempts": '{"not": "an array"}',
    "event_type": "chat.transcript",
}


@pytest.mark.parametrize("column", sorted(BAD_INSERTS))
def test_live_database_rejects_non_metadata(live_app: Settings, column: str) -> None:
    # Bypasses the Event model: the table's own CHECK constraints must refuse it.
    async def body(conn: AsyncConnection) -> str:
        await require_events_table(conn)
        nested = await conn.begin_nested()
        try:
            await conn.execute(text(BAD_INSERTS[column]), {"v": BAD_VALUES[column]})
        except DBAPIError as exc:
            await nested.rollback()
            return type(exc.orig).__name__ + " " + str(exc.orig)
        await nested.rollback()
        return "accepted"

    result = in_rolled_back_tx(live_app, body)
    assert "CheckViolation" in result or "check constraint" in result


SNAPSHOT = """
SELECT 'rel:' || c.relname || ':' || c.relkind::text FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema
UNION ALL
SELECT 'fn:' || p.proname FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = :schema
UNION ALL
SELECT 'trg:' || t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = :schema AND NOT t.tgisinternal
"""


def test_live_migration_round_trip_in_throwaway_schema(live_owner: Settings) -> None:
    schema = f"aether_migtest_{uuid.uuid4().hex[:12]}"
    config = Config(str(REPO / "alembic.ini"))
    # A row written before 0003 must come through it untouched: same id, and the same
    # xmin (the transaction that wrote it), so the row was not rewritten.
    row_sql = "SELECT id::text || ':' || xmin::text FROM events"
    app_can = (
        "SELECT 'insert:' || has_table_privilege('aether_app', 'events', 'INSERT')"
        " || ' update:' || has_table_privilege('aether_app', 'events', 'UPDATE')"
    )

    def snapshot(sync: Connection, name: str) -> set[str]:
        return {r[0] for r in sync.execute(text(SNAPSHOT), {"schema": name})}

    def rows(sync: Connection, sql: str) -> set[str]:
        return {r[0] for r in sync.execute(text(sql))}

    def migrate(sync: Connection) -> dict[str, set[str]]:
        public_before = snapshot(sync, "public")
        sync.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config.attributes["connection"] = sync

        command.upgrade(config, "0001")
        at_0001 = snapshot(sync, schema)
        command.upgrade(config, "0002")
        at_0002 = snapshot(sync, schema)
        sync.execute(text(_BASE + ") VALUES (now(), 'request.received', 1, 'core')"))
        row_at_0002 = rows(sync, row_sql)
        command.upgrade(config, "0003")
        at_0003 = snapshot(sync, schema)
        row_at_0003 = rows(sync, row_sql)
        app_at_0003 = rows(sync, app_can)
        command.downgrade(config, "0002")
        back_at_0002 = snapshot(sync, schema)
        app_back_at_0002 = rows(sync, app_can)
        command.downgrade(config, "0001")
        back_at_0001 = snapshot(sync, schema)
        return {
            "public_before": public_before,
            "public_after": snapshot(sync, "public"),
            "at_0001": at_0001,
            "at_0002": at_0002,
            "at_0003": at_0003,
            "back_at_0002": back_at_0002,
            "back_at_0001": back_at_0001,
            "row_at_0002": row_at_0002,
            "row_at_0003": row_at_0003,
            "app_at_0003": app_at_0003,
            "app_back_at_0002": app_back_at_0002,
        }

    async def body(conn: AsyncConnection) -> dict[str, set[str]]:
        result: dict[str, set[str]] = await conn.run_sync(migrate)
        return result

    snaps = in_rolled_back_tx(live_owner, body, owner=True)

    added = snaps["at_0002"] - snaps["at_0001"]
    assert added == {
        "rel:events:r",
        "rel:events_id_seq:S",
        "rel:events_pkey:i",
        "rel:events_occurred_at_idx:i",
        "rel:events_request_id_idx:i",
        "fn:events_append_only",
        "trg:events_no_update_delete",
        "trg:events_no_truncate",
    }
    assert snaps["at_0001"] <= snaps["at_0002"]  # nothing from 0001 removed or changed
    # 0003 only grants and revokes: no object added, dropped or recreated...
    assert snaps["at_0003"] == snaps["at_0002"]
    # ...and the existing row is the same row, not a rewritten copy.
    assert snaps["row_at_0003"] == snaps["row_at_0002"]
    assert len(snaps["row_at_0002"]) == 1
    assert snaps["app_at_0003"] == {"insert:true update:false"}
    assert snaps["app_back_at_0002"] == {"insert:false update:false"}
    assert snaps["back_at_0002"] == snaps["at_0002"]
    assert snaps["back_at_0001"] == snaps["at_0001"]  # downgrade removes exactly that
    assert snaps["public_after"] == snaps["public_before"]  # real schema untouched
