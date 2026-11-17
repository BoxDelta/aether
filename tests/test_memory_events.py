"""Memory events (ADR-024 §5, #72): the Event model, the typed writer and migration
0005, without a database. Live tests: tests/test_memory_events_live.py and
tests/test_memory_store_live.py."""

import asyncio
import importlib.util
import re
from types import ModuleType
from typing import Any, get_args
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

import core.config
from core import events as events_module
from core.config import Settings
from core.events import (
    MAX_MEMORY_RECORD_IDS,
    MEMORY_ACTORS,
    MEMORY_COUNT_FIELDS,
    MEMORY_EVENT_TYPES,
    MEMORY_RECORD_EVENT_TYPES,
    Event,
    EventType,
    MemoryRecordMeta,
    events_table,
    memory_record_event,
    write_memory_event,
)
from memory import types as memory_types
from router.contract import NON_SECRET_LEVELS
from tests.test_events import statements
from tests.test_privacy_levels import REPO

MIGRATION = REPO / "migrations/versions/0005_memory_events.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0005", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def meta(**changes: Any) -> MemoryRecordMeta:
    values: dict[str, Any] = {
        "record_id": uuid4(),
        "category": "preference",
        "source": "owner",
        "level": "personal",
    }
    return MemoryRecordMeta(**{**values, **changes})


def retrieved(**changes: Any) -> dict[str, Any]:
    ids = (uuid4(), uuid4(), uuid4())
    values: dict[str, Any] = {
        "event_type": "memory.context.retrieved",
        "actor": "memory",
        "request_id": "req-1",
        "provider": "ollama",
        "memory_count_public": 2,
        "memory_count_personal": 1,
        "memory_count_sensitive": 0,
        "memory_chars": 420,
        "memory_effective_level": "personal",
        "memory_record_ids": ids,
        "memory_left_out": 4,
    }
    return {**values, **changes}


# --- frozen copies in 0005 match the code ---------------------------------------------


def test_migration_0005_matches_the_code() -> None:
    m = _migration()
    assert _flatten(EventType) == [*m.BASE_EVENT_TYPES, *m.MEMORY_EVENT_TYPES]
    assert m.MEMORY_EVENT_TYPES == MEMORY_EVENT_TYPES
    assert m.RECORD_EVENT_TYPES == MEMORY_RECORD_EVENT_TYPES
    assert (*m.BASE_ACTORS, *m.MEMORY_ACTORS) == get_args(events_module.Actor)
    assert m.MEMORY_ACTORS == MEMORY_ACTORS
    assert m.CATEGORIES == memory_types.CATEGORIES
    assert m.SOURCES == memory_types.SOURCES
    assert m.LEVELS == NON_SECRET_LEVELS
    assert m.MAX_RECORD_IDS == MAX_MEMORY_RECORD_IDS
    assert m.COUNT_COLUMNS == MEMORY_COUNT_FIELDS


def _flatten(literal: Any) -> list[str]:
    out: list[str] = []
    for arg in get_args(literal):
        out.extend(_flatten(arg) if get_args(arg) else [arg])
    return out


def test_base_event_types_are_the_0002_list() -> None:
    old = (REPO / "migrations/versions/0002_events.py").read_text()
    block = re.search(r"EVENT_TYPES = \(([^)]*)\)", old)
    assert block is not None
    assert tuple(re.findall(r'"([\w.]+)"', block.group(1))) == _migration().BASE_EVENT_TYPES
    assert set(_flatten(EventType)) == {*_migration().BASE_EVENT_TYPES, *MEMORY_EVENT_TYPES}


def test_table_view_has_exactly_the_0005_columns() -> None:
    added = {c for c in events_table.c.keys() if c.startswith("memory_")}  # noqa: SIM118
    assert added == set(_migration().COLUMNS)
    assert {f for f in Event.model_fields if f.startswith("memory_")} == added


def test_count_fields_follow_the_level_order() -> None:
    assert tuple(f"memory_count_{level}" for level in NON_SECRET_LEVELS) == MEMORY_COUNT_FIELDS
    assert all(f in Event.model_fields for f in MEMORY_COUNT_FIELDS)


NEVER = ("content", "key", "query", "reason", "text", "message", "note", "json")


def test_no_memory_column_can_hold_content() -> None:
    # Typed, short-valued columns only: no free text, no JSON (ADR-024 §5).
    for name in _migration().COLUMNS:
        assert not any(word in name for word in NEVER), name
    source = MIGRATION.read_text()
    assert "JSONB" not in source and "sa.JSON" not in source and "postgresql.JSON" not in source
    assert "sa.Text" in source  # the level/category/source columns...
    for name in ("memory_category", "memory_source", "memory_level", "memory_old_level"):
        assert name in _migration().CHECKS[f"events_{name}"]  # ...each limited to a fixed list
    assert "memory_effective_level" in _migration().CHECKS["events_memory_effective_level"]


# --- the Event model mirrors the CHECKs -------------------------------------------------


@pytest.mark.parametrize("kind", MEMORY_RECORD_EVENT_TYPES)
def test_record_events_build(kind: Any) -> None:
    m = meta()
    event = memory_record_event(kind, m)
    assert event.actor == "memory"
    assert (event.memory_record_id, event.memory_category) == (m.record_id, m.category)
    assert (event.memory_source, event.memory_level) == (m.source, m.level)
    assert event.memory_old_level is None


def test_updated_carries_the_old_level_only_when_it_changed() -> None:
    event = memory_record_event("memory.updated", meta(level="sensitive"), old_level="personal")
    assert (event.memory_old_level, event.memory_level) == ("personal", "sensitive")
    with pytest.raises(ValidationError):
        memory_record_event("memory.updated", meta(level="personal"), old_level="personal")
    with pytest.raises(ValidationError):
        memory_record_event("memory.approved", meta(level="sensitive"), old_level="personal")


def test_restore_actor_allowed_on_memory_events() -> None:
    assert memory_record_event("memory.deleted", meta(), actor="restore").actor == "restore"


def test_retrieved_and_exported_events_validate() -> None:
    assert Event(**retrieved()).memory_left_out == 4
    exported = Event(event_type="memory.exported", actor="memory", memory_export_count=0)
    assert exported.memory_export_count == 0


BAD_EVENTS: dict[str, dict[str, Any]] = {
    "secret level": {"event_type": "memory.deleted", "memory_level": "secret"},
    "bad category": {"event_type": "memory.deleted", "memory_category": "operational"},
    "bad source": {"event_type": "memory.deleted", "memory_source": "user"},
    "memory event as core": {"event_type": "memory.deleted", "actor": "core"},
    "record without id": {"event_type": "memory.deleted", "memory_record_id": None},
    "record fields on request.received": {"event_type": "request.received", "actor": "core"},
    "record fields on exported": {"event_type": "memory.exported", "memory_export_count": 1},
    "negative export": {
        "event_type": "memory.exported",
        "memory_export_count": -1,
        "memory_record_id": None,
        "memory_category": None,
        "memory_source": None,
        "memory_level": None,
    },
    "content smuggled": {"event_type": "memory.deleted", "content": "x"},
}


@pytest.mark.parametrize("name", sorted(BAD_EVENTS))
def test_bad_record_events_refused(name: str) -> None:
    base: dict[str, Any] = {
        "event_type": "memory.proposed",
        "actor": "memory",
        "memory_record_id": uuid4(),
        "memory_category": "task",
        "memory_source": "model",
        "memory_level": "public",
    }
    with pytest.raises(ValidationError):
        Event(**{**base, **BAD_EVENTS[name]})


BAD_RETRIEVED: dict[str, dict[str, Any]] = {
    "21 ids": {
        "memory_record_ids": tuple(uuid4() for _ in range(21)),
        "memory_count_public": 21,
        "memory_count_personal": 0,
    },
    "ids do not match counts": {"memory_count_public": 3},
    "effective below sent": {"memory_effective_level": "public"},
    "secret effective": {"memory_effective_level": "secret"},
    "no request id": {"request_id": None},
    "no provider": {"provider": None},
    "missing chars": {"memory_chars": None},
    "negative left out": {"memory_left_out": -1},
    "record id too": {"memory_record_id": uuid4()},
    "export count too": {"memory_export_count": 1},
    "as router": {"actor": "router"},
}


@pytest.mark.parametrize("name", sorted(BAD_RETRIEVED))
def test_bad_retrieved_events_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        Event(**retrieved(**BAD_RETRIEVED[name]))


@pytest.mark.parametrize(
    "field", ["memory_count_public", "memory_left_out", "memory_record_ids", "memory_old_level"]
)
def test_memory_fields_refused_on_other_events(field: str) -> None:
    value: Any = {"memory_record_ids": (), "memory_old_level": "public"}.get(field, 0)
    with pytest.raises(ValidationError):
        Event(event_type="request.received", actor="core", **{field: value})


def test_old_events_still_build() -> None:
    assert Event(event_type="request.received", actor="core").memory_record_id is None
    with pytest.raises(ValidationError):
        Event(event_type="request.received", actor="memory")


def test_record_meta_has_no_place_for_content() -> None:
    assert set(MemoryRecordMeta.__slots__) == {"record_id", "category", "source", "level"}


# --- the writer ---------------------------------------------------------------------------


class _Conn:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)

        class _Result:
            def scalar_one(self) -> int:
                return 7

        return _Result()


def test_writer_inserts_on_the_given_connection() -> None:
    conn = _Conn()
    event = memory_record_event("memory.proposed", meta(source="model"))
    assert asyncio.run(write_memory_event(conn, event)) == 7  # type: ignore[arg-type]
    (statement,) = conn.statements
    params = statement.compile().params
    assert params["event_type"] == "memory.proposed"
    assert params["actor"] == "memory"
    assert params["memory_source"] == "model"


def test_writer_refuses_non_memory_events_and_free_text() -> None:
    conn = _Conn()
    with pytest.raises(ValueError, match="not a memory event"):
        asyncio.run(write_memory_event(conn, Event(event_type="request.received", actor="core")))  # type: ignore[arg-type]
    leaky = memory_record_event("memory.deleted", meta()).model_copy(
        update={"error_message": "anything"}
    )
    with pytest.raises(ValueError, match="no free text"):
        asyncio.run(write_memory_event(conn, leaky))  # type: ignore[arg-type]
    assert conn.statements == []


# --- the migration, offline ---------------------------------------------------------------


def _offline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], direction: str
) -> list[str]:
    url = "postgresql+asyncpg://aether:offline-test-pw@127.0.0.1:5432/aether"
    settings = Settings(_env_file=None, database_url=url)  # type: ignore[arg-type]
    monkeypatch.setattr(core.config, "get_settings", lambda: settings)
    config = Config(str(REPO / "alembic.ini"))
    if direction == "upgrade":
        command.upgrade(config, "0004:0005", sql=True)
    else:
        command.downgrade(config, "0005:0004", sql=True)
    stmts = statements(capsys.readouterr().out)
    return [s for s in stmts if not s.startswith(("BEGIN", "COMMIT", "UPDATE alembic_version"))]


def test_upgrade_only_adds_columns_and_checks_to_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ddl = _offline(monkeypatch, capsys, "upgrade")
    for s in ddl:
        assert s.startswith("ALTER TABLE events "), s[:80]
        assert re.match(
            r"ALTER TABLE events (ADD COLUMN memory_\w+ |DROP CONSTRAINT |ADD CONSTRAINT )", s
        ), s[:80]
        if " ADD COLUMN " in s:  # nullable, no default: existing rows aren't rewritten
            assert " DEFAULT " not in s and " NOT NULL" not in s, s
    added = [re.match(r"ALTER TABLE events ADD COLUMN (\w+)", s) for s in ddl]
    assert {m.group(1) for m in added if m} == set(_migration().COLUMNS)
    dropped = [s for s in ddl if "DROP CONSTRAINT" in s]
    assert dropped == [
        "ALTER TABLE events DROP CONSTRAINT events_event_type",
        "ALTER TABLE events DROP CONSTRAINT events_actor",
    ]
    joined = " ".join(ddl)
    for word in ("GRANT", "REVOKE", "TRIGGER", "FUNCTION", "DROP TABLE", "DELETE", "UPDATE"):
        assert word not in joined, word


def test_downgrade_refuses_before_changing_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ddl = _offline(monkeypatch, capsys, "downgrade")
    assert ddl[0].startswith("DO $$")  # the memory-rows check comes first
    joined = " ".join(ddl)
    assert "DELETE" not in joined and "TRUNCATE" not in joined  # never deletes audit rows
    assert "GRANT" not in joined and "REVOKE" not in joined
    dropped = {
        m.group(1) for s in ddl if (m := re.match(r"ALTER TABLE events DROP COLUMN (\w+)", s))
    }
    assert dropped == set(_migration().COLUMNS)
