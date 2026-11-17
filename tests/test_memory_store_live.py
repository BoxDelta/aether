"""The memory store against a live Postgres, as the app role (#69, ADR-024). Opt-in.

Every test runs through Core's own engine (``core.db.create_engine``) with a
throwaway member of ``aether_app``, so the privilege check runs on each connection.
The store commits, so the ``store`` fixture deletes (as the owner) every record a
test created. All content and keys are invented canaries.

Events (ADR-024 §5, #72): every change writes one ``memory.*`` event in the same
transaction. Events are append-only, so the events these tests write stay in the
test database (a throwaway container or Sentinel's service database); they hold
IDs, categories, sources and levels only.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from core.config import Settings
from core.db import create_engine, create_migration_engine
from core.log import SecretCheck
from memory.store import (
    Approval,
    InvalidRecord,
    KeyConflict,
    ListFilter,
    MemoryRecord,
    MemoryStore,
    MemoryStoreError,
    NewRecord,
    NotAllowed,
    NotPending,
    RecordChanges,
    RecordNotFound,
    SecretContent,
)
from tests.conftest import LIVE_DB
from tests.fake_secrets import by_family

pytestmark = LIVE_DB

T = TypeVar("T")
PREFIX = "canary-memlive-5d21"
KEY = "canary.memlive_5d21"  # gitleaks:allow (invented memory key, not a secret)
OTHER_KEY = "canary.memlive_other"
SNAPSHOT_SQL = "SELECT row_to_json(m)::text FROM public.memory_records m ORDER BY id"


def content(n: int | str) -> str:
    return f"{PREFIX} invented fact {n}"


Run = Callable[[Callable[[MemoryStore], Awaitable[T]]], T]


async def _owner_sql(owner: Settings, sql: str) -> list[Any]:
    engine = create_migration_engine(owner)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            return list(result.scalars()) if result.returns_rows else []
    finally:
        await engine.dispose()


def owner_sql(owner: Settings, sql: str) -> list[Any]:
    return asyncio.run(_owner_sql(owner, sql))


def snapshot(owner: Settings) -> list[str]:
    return owner_sql(owner, SNAPSHOT_SQL)


def last_event_id(owner: Settings) -> int:
    return int(owner_sql(owner, "SELECT coalesce(max(id), 0) FROM events")[0])


def events_after(owner: Settings, after: int) -> list[dict[str, Any]]:
    """Every event written after ``after``, every column, as JSON (oldest first)."""
    sql = f"SELECT row_to_json(e)::text FROM events e WHERE id > {int(after)} ORDER BY id"  # noqa: S608
    return [json.loads(row) for row in owner_sql(owner, sql)]


@pytest.fixture
def store(live_app: Settings, live_owner: Settings) -> Iterator[Run[Any]]:
    """``run(body)`` runs ``body(store)`` with a fresh app engine. Afterwards every
    record the test created is deleted as the owner."""
    tables = owner_sql(live_owner, "SELECT to_regclass('public.memory_records')::text")
    if tables != ["memory_records"]:
        pytest.fail("memory_records missing: run `uv run alembic upgrade head`")
    before = set(owner_sql(live_owner, "SELECT id::text FROM public.memory_records"))

    def run(body: Callable[[MemoryStore], Awaitable[T]]) -> T:
        async def go() -> T:
            engine = create_engine(live_app)
            try:
                # Built from Settings, as every caller must (#70).
                return await body(MemoryStore(engine, SecretCheck.from_settings(live_app)))
            finally:
                await engine.dispose()

        return asyncio.run(go())

    try:
        yield run
    finally:
        after = set(owner_sql(live_owner, "SELECT id::text FROM public.memory_records"))
        created = after - before
        if created:
            ids = ", ".join(f"'{UUID(i)}'" for i in created)
            owner_sql(live_owner, f"DELETE FROM public.memory_records WHERE id IN ({ids})")  # noqa: S608


def owner_record(n: int | str = 1, **changes: Any) -> NewRecord:
    values: dict[str, Any] = {
        "content": content(n),
        "category": "preference",
        "privacy": "public",
        "source": "owner",
    }
    return NewRecord(**{**values, **changes})


def proposal(n: int | str = 1, **changes: Any) -> NewRecord:
    values: dict[str, Any] = {
        "content": content(n),
        "category": "project",
        "privacy": "personal",
        "source": "model",
        "source_provider": "ollama",
        "source_request_id": "req-memlive.1",
    }
    return NewRecord(**{**values, **changes})


# --- create and get ------------------------------------------------------------------


def test_runs_as_the_app_role(store: Run[Any], live_app: Settings) -> None:
    async def body(_: MemoryStore) -> str:
        engine = create_engine(live_app)
        try:
            async with engine.connect() as conn:
                return str(await conn.scalar(text("SELECT current_user")))
        finally:
            await engine.dispose()

    assert store(body).startswith("aether_app_test_")


def test_owner_record_is_approved_with_the_owners_level(store: Run[Any]) -> None:
    async def body(s: MemoryStore) -> tuple[MemoryRecord, MemoryRecord]:
        made = await s.create(owner_record(key=KEY, privacy="sensitive"))
        return made, await s.get(made.id)

    made, read = store(body)
    assert made == read
    assert (made.status, made.privacy, made.privacy_source, made.source) == (
        "approved",
        "sensitive",
        "owner",
        "owner",
    )
    assert made.approved_at is not None and made.content == content(1) and made.key == KEY
    assert made.created_at == made.updated_at == made.approved_at


@pytest.mark.parametrize("source", ["model", "tool"])
def test_model_and_tool_records_are_always_pending(store: Run[Any], source: str) -> None:
    async def body(s: MemoryStore) -> MemoryRecord:
        return await s.create(proposal(source=source, privacy="public"))

    made = store(body)
    assert (made.status, made.privacy, made.privacy_source, made.source) == (
        "pending",
        "public",
        "suggested",
        source,
    )
    assert made.approved_at is None
    assert (made.source_provider, made.source_request_id) == ("ollama", "req-memlive.1")


def test_get_unknown_id_is_not_found(store: Run[Any]) -> None:
    missing = uuid4()
    with pytest.raises(RecordNotFound) as info:
        store(lambda s: s.get(missing))
    assert str(missing) in str(info.value)


# --- list ----------------------------------------------------------------------------


def test_list_filters(store: Run[Any]) -> None:
    async def body(s: MemoryStore) -> dict[str, list[str]]:
        await s.create(owner_record("a", key=KEY, category="profile", privacy="personal"))
        await s.create(owner_record("b", category="task", privacy="sensitive"))
        await s.create(proposal("c", key=KEY, category="profile"))
        await s.create(proposal("d", source="tool", category="task", privacy="public"))

        async def find(**f: Any) -> list[str]:
            return sorted(
                r.content[-1] for r in await s.list(ListFilter(**f)) if PREFIX in r.content
            )

        return {
            "all": await find(),
            "pending": await find(status="pending"),
            "approved": await find(status="approved"),
            "personal": await find(privacy="personal"),
            "task": await find(category="task"),
            "key": await find(key=KEY),
            "key+approved": await find(key=KEY, status="approved"),
            "none": await find(key=OTHER_KEY),
        }

    assert store(body) == {
        "all": ["a", "b", "c", "d"],
        "pending": ["c", "d"],
        "approved": ["a", "b"],
        "personal": ["a", "c"],
        "task": ["b", "d"],
        "key": ["a", "c"],
        "key+approved": ["a"],
        "none": [],
    }


def test_list_pages_newest_first(store: Run[Any]) -> None:
    async def body(s: MemoryStore) -> list[list[str]]:
        for n in range(5):
            await s.create(owner_record(n, category="decision"))
        pages = []
        for offset in (0, 2, 4):
            found = await s.list(ListFilter(category="decision", limit=2, offset=offset))
            pages.append([r.content for r in found])
        return pages

    assert store(body) == [[content(4), content(3)], [content(2), content(1)], [content(0)]]


def test_expired_records_are_left_out_unless_asked_for(store: Run[Any]) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(days=1)

    async def body(s: MemoryStore) -> dict[str, Any]:
        old = await s.create(owner_record("old", expires_at=past, category="reference"))
        await s.create(owner_record("new", expires_at=future, category="reference"))
        await s.create(owner_record("forever", category="reference"))
        names = [r.content.split()[-1] for r in await s.list(ListFilter(category="reference"))]
        everything = await s.list(ListFilter(category="reference", include_expired=True))
        results: dict[str, Any] = {
            "default": sorted(names),
            "included": sorted(r.content.split()[-1] for r in everything),
            "get with flag": (await s.get(old.id, include_expired=True)).id == old.id,
        }
        for name, call in {
            "get": s.get(old.id),
            "update": s.update(old.id, RecordChanges(category="task"), actor="owner"),
        }.items():
            try:
                await call
                results[name] = "found"
            except RecordNotFound:
                results[name] = "not found"
        await s.delete(old.id, actor="owner")  # expired records can still be deleted
        left = await s.list(ListFilter(category="reference", include_expired=True))
        results["deleted"] = old.id not in {r.id for r in left}
        return results

    assert store(body) == {
        "default": ["forever", "new"],
        "included": ["forever", "new", "old"],
        "get with flag": True,
        "get": "not found",
        "update": "not found",
        "deleted": True,
    }


def test_expired_pending_record_cannot_be_approved(store: Run[Any]) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)

    async def body(s: MemoryStore) -> MemoryRecord:
        made = await s.create(proposal(expires_at=past))
        await s.approve(made.id, Approval("public"), actor="owner")
        return made

    with pytest.raises(RecordNotFound):
        store(body)


# --- update --------------------------------------------------------------------------


def test_owner_corrects_a_record(store: Run[Any]) -> None:
    async def body(s: MemoryStore) -> tuple[MemoryRecord, MemoryRecord]:
        made = await s.create(owner_record(key=KEY))
        changed = await s.update(
            made.id,
            RecordChanges(content=content("fixed"), key=None, category="task", privacy="personal"),
            actor="owner",
        )
        return made, changed

    made, changed = store(body)
    assert (changed.content, changed.key, changed.category, changed.privacy) == (
        content("fixed"),
        None,
        "task",
        "personal",
    )
    assert (changed.status, changed.privacy_source, changed.approved_at) == (
        "approved",
        "owner",
        made.approved_at,
    )
    assert changed.created_at == made.created_at and changed.updated_at > made.updated_at


def test_a_level_set_on_a_pending_record_is_the_owners(store: Run[Any]) -> None:
    async def body(s: MemoryStore) -> tuple[MemoryRecord, MemoryRecord]:
        made = await s.create(proposal())
        content_only = await s.update(made.id, RecordChanges(content=content(2)), actor="owner")
        level = await s.update(made.id, RecordChanges(privacy="sensitive"), actor="owner")
        return content_only, level

    content_only, level = store(body)
    assert (content_only.status, content_only.privacy_source) == ("pending", "suggested")
    assert (level.status, level.privacy, level.privacy_source) == ("pending", "sensitive", "owner")
    assert level.approved_at is None


def test_update_unknown_id_is_not_found(store: Run[Any]) -> None:
    with pytest.raises(RecordNotFound):
        store(lambda s: s.update(uuid4(), RecordChanges(category="task"), actor="owner"))


# --- approve -------------------------------------------------------------------------


def test_owner_approves_with_the_final_level_and_corrections(store: Run[Any]) -> None:
    async def body(s: MemoryStore) -> tuple[MemoryRecord, MemoryRecord]:
        made = await s.create(proposal(privacy="public"))
        approved = await s.approve(
            made.id,
            Approval("sensitive", content=content("approved"), key=KEY, category="decision"),
            actor="owner",
        )
        return made, approved

    made, approved = store(body)
    assert (approved.status, approved.privacy, approved.privacy_source) == (
        "approved",
        "sensitive",
        "owner",
    )
    assert (approved.content, approved.key, approved.category) == (
        content("approved"),
        KEY,
        "decision",
    )
    assert approved.approved_at is not None and approved.approved_at > made.created_at
    assert (approved.source, approved.source_provider) == ("model", "ollama")  # provenance kept


def test_approving_an_approved_record_is_refused(store: Run[Any], live_owner: Settings) -> None:
    made = store(lambda s: s.create(owner_record()))
    before = snapshot(live_owner)
    with pytest.raises(NotPending):
        store(lambda s: s.approve(made.id, Approval("sensitive"), actor="owner"))
    assert snapshot(live_owner) == before


def test_approve_unknown_id_is_not_found(store: Run[Any]) -> None:
    with pytest.raises(RecordNotFound):
        store(lambda s: s.approve(uuid4(), Approval("public"), actor="owner"))


# --- delete --------------------------------------------------------------------------


def test_delete_is_a_hard_delete(store: Run[Any], live_owner: Settings) -> None:
    made = store(lambda s: s.create(proposal()))
    store(lambda s: s.delete(made.id, actor="owner"))
    sql = f"SELECT count(*) FROM public.memory_records WHERE id = '{made.id}'"  # noqa: S608
    rows = owner_sql(live_owner, sql)
    assert rows == [0]
    with pytest.raises(RecordNotFound):
        store(lambda s: s.get(made.id, include_expired=True))
    with pytest.raises(RecordNotFound):
        store(lambda s: s.delete(made.id, actor="owner"))


# --- one approved record per key -----------------------------------------------------


def test_key_conflicts_are_store_errors_and_change_nothing(
    store: Run[Any], live_owner: Settings
) -> None:
    first = store(lambda s: s.create(owner_record(1, key=KEY)))
    other = store(lambda s: s.create(owner_record(2, key=OTHER_KEY)))
    pending = store(lambda s: s.create(proposal(3, key=KEY)))  # pending may share a key
    before = snapshot(live_owner)
    attempts: dict[str, Callable[[MemoryStore], Awaitable[Any]]] = {
        "create": lambda s: s.create(owner_record(4, key=KEY)),
        "update": lambda s: s.update(other.id, RecordChanges(key=KEY), actor="owner"),
        "approve": lambda s: s.approve(pending.id, Approval("public"), actor="owner"),
    }
    for name, attempt in attempts.items():
        with pytest.raises(KeyConflict) as info:
            store(attempt)
        exc = info.value
        assert KEY not in str(exc) and PREFIX not in str(exc), name
        assert exc.__cause__ is None and exc.__context__ is None, name
    assert snapshot(live_owner) == before
    assert store(lambda s: s.get(first.id)).key == KEY


# --- a failing operation leaves the table unchanged ----------------------------------


def _bypass(record: Any, **values: Any) -> Any:
    """Skip the store's own checks, to reach the database CHECKs (the backstop)."""
    for name, value in values.items():
        object.__setattr__(record, name, value)
    return record


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"privacy": "secret"}, "memory_records_privacy"),
        ({"content": "x" * 1001}, "memory_records_content"),
        ({"key": "Bad Key"}, "memory_records_key"),
        ({"category": "operational"}, "memory_records_category"),
    ],
)
def test_database_checks_back_up_the_store(
    store: Run[Any], live_owner: Settings, values: dict[str, Any], constraint: str
) -> None:
    before = snapshot(live_owner)
    record = _bypass(owner_record(), **values)
    with pytest.raises(InvalidRecord) as info:
        store(lambda s: s.create(record))
    exc = info.value
    assert str(exc) == f"record: refused by database check {constraint}"
    assert exc.__cause__ is None and exc.__context__ is None
    assert snapshot(live_owner) == before


def test_failed_updates_leave_the_table_unchanged(store: Run[Any], live_owner: Settings) -> None:
    made = store(lambda s: s.create(proposal(key=KEY)))
    before = snapshot(live_owner)
    bad_update = _bypass(RecordChanges(content=content("x")), category="operational")
    bad_approval = _bypass(Approval("public", content=content("y")), privacy="secret")
    for attempt, error in [
        (lambda s: s.update(made.id, bad_update, actor="owner"), InvalidRecord),
        (lambda s: s.approve(made.id, bad_approval, actor="owner"), InvalidRecord),
        (lambda s: s.update(made.id, RecordChanges(content="z"), actor="model"), NotAllowed),
        (lambda s: s.approve(made.id, Approval("public"), actor="tool"), NotAllowed),
        (lambda s: s.delete(made.id, actor="model"), NotAllowed),
    ]:
        with pytest.raises(error):
            store(attempt)
    assert snapshot(live_owner) == before


# --- secret-shaped content is refused before the database (#70) ---------------------


def test_refused_secrets_change_neither_table_nor_events(
    store: Run[Any], live_app: Settings, live_owner: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # The configured value is this run's own throwaway app-role password (from
    # live_app's DATABASE_URL), taken explicitly: live_app also carries the owner's
    # migration URL, whose password must never be used as a test value.
    assert live_app.database_url is not None
    password = urlsplit(live_app.database_url.get_secret_value()).password
    assert password, "live_app's DATABASE_URL has no password"
    made = store(lambda s: s.create(owner_record(1)))
    pending = store(lambda s: s.create(proposal(2)))
    start = last_event_id(live_owner)
    before = snapshot(live_owner)
    caplog.set_level(logging.DEBUG)
    for fake in by_family(password):
        for attempt in (
            lambda s, t=fake.text: s.create(owner_record(3, content=t)),
            lambda s, t=fake.text: s.create(proposal(4, content=t)),
            lambda s, t=fake.text: s.update(made.id, RecordChanges(content=t), actor="owner"),
            lambda s, t=fake.text: s.approve(
                pending.id, Approval("public", content=t), actor="owner"
            ),
        ):
            with pytest.raises(SecretContent) as info:
                store(attempt)
            exc = info.value
            assert exc.__cause__ is None and exc.__context__ is None
            # Value-free assertions: a failure must never print the secret.
            echoed = any(fake.core in shown for shown in (str(exc), repr(exc), caplog.text))
            assert not echoed, f"{fake.family}: the error or the logs echo the secret"
    unchanged_events = events_after(live_owner, start) == []
    unchanged_table = snapshot(live_owner) == before
    assert unchanged_events, "a refused write left an event"
    assert unchanged_table, "a refused write changed memory_records"


# --- no content or key in logs or events ---------------------------------------------


def test_no_content_or_key_in_logs_errors_or_events(
    store: Run[Any], live_owner: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    events_before = last_event_id(live_owner)
    errors: list[str] = []

    async def body(s: MemoryStore) -> None:
        a = await s.create(owner_record(1, key=KEY))
        b = await s.create(proposal(2, key=KEY))
        await s.get(a.id)
        await s.list(ListFilter(key=KEY))
        await s.update(a.id, RecordChanges(content=content(3), key=OTHER_KEY), actor="owner")
        await s.approve(b.id, Approval("personal", content=content(4)), actor="owner")
        for failing in (
            s.create(owner_record(5, key=KEY)),  # key conflict
            s.update(b.id, RecordChanges(key=OTHER_KEY), actor="owner"),  # key conflict
            s.create(_bypass(owner_record(6, key=KEY), privacy="secret")),  # database CHECK
            s.update(a.id, RecordChanges(content=content(7)), actor="model"),
        ):
            try:
                await failing
            except MemoryStoreError as exc:
                errors.append(f"{type(exc).__name__}: {exc!r} {exc}")
        await s.delete(a.id, actor="owner")
        await s.delete(b.id, actor="owner")

    with caplog.at_level(logging.DEBUG):
        store(body)

    assert len(errors) == 4
    store_logs = [r for r in caplog.records if r.name == "aether.memory.store"]
    assert [r.getMessage() for r in store_logs] == [
        "Memory record created",
        "Memory record created",
        "Memory record updated",
        "Memory record approved",
        "Memory record deleted",
        "Memory record deleted",
    ]
    rendered = errors + [f"{r.getMessage()} {vars(r)!r} {r.exc_text or ''}" for r in caplog.records]
    for line in rendered:
        assert PREFIX not in line and KEY not in line and OTHER_KEY not in line, line
    # One event per successful change (2 creates, update, approve, 2 deletes), none for
    # the failures; no column of any of them holds content or a key.
    written = events_after(live_owner, events_before)
    assert [e["event_type"] for e in written] == [
        "memory.approved",
        "memory.proposed",
        "memory.updated",
        "memory.approved",
        "memory.deleted",
        "memory.deleted",
    ]
    dumped = json.dumps(written)
    for canary in (PREFIX, KEY, OTHER_KEY):
        assert canary not in dumped


# --- events (ADR-024 §5, #72) -----------------------------------------------------------

RECORD_EVENT_KEYS = {
    "event_type",
    "actor",
    "memory_record_id",
    "memory_category",
    "memory_source",
    "memory_level",
    "memory_old_level",
}


def only_event(owner: Settings, after: int) -> dict[str, Any]:
    written = events_after(owner, after)
    assert len(written) == 1, [e["event_type"] for e in written]
    event = written[0]
    # Every other column is empty: no request text, no counts, no free text.
    others = {k: v for k, v in event.items() if k not in RECORD_EVENT_KEYS}
    assert {k for k, v in others.items() if v is not None} <= {
        "id",
        "occurred_at",
        "recorded_at",
        "schema_version",
    }, others
    return event


def test_each_operation_writes_exactly_one_event(store: Run[Any], live_owner: Settings) -> None:
    start = last_event_id(live_owner)
    proposed = store(lambda s: s.create(proposal(1, key=KEY)))
    e = only_event(live_owner, start)
    assert e["event_type"] == "memory.proposed" and e["actor"] == "memory"
    assert e["memory_record_id"] == str(proposed.id)
    assert (e["memory_category"], e["memory_source"], e["memory_level"]) == (
        "project",
        "model",
        "personal",
    )
    assert e["memory_old_level"] is None

    start = last_event_id(live_owner)
    owned = store(lambda s: s.create(owner_record(2)))
    e = only_event(live_owner, start)
    assert e["event_type"] == "memory.approved"  # owner create: approved at once (D1)
    assert (e["memory_record_id"], e["memory_source"], e["memory_level"]) == (
        str(owned.id),
        "owner",
        "public",
    )

    start = last_event_id(live_owner)
    store(lambda s: s.approve(proposed.id, Approval("sensitive", category="task"), actor="owner"))
    e = only_event(live_owner, start)
    assert e["event_type"] == "memory.approved"
    assert (e["memory_record_id"], e["memory_category"], e["memory_level"]) == (
        str(proposed.id),
        "task",
        "sensitive",  # the owner's final level, not the suggestion
    )
    assert e["memory_source"] == "model" and e["memory_old_level"] is None

    start = last_event_id(live_owner)
    store(lambda s: s.update(owned.id, RecordChanges(privacy="personal"), actor="owner"))
    e = only_event(live_owner, start)
    assert e["event_type"] == "memory.updated"
    assert (e["memory_old_level"], e["memory_level"]) == ("public", "personal")

    start = last_event_id(live_owner)
    store(lambda s: s.update(owned.id, RecordChanges(content=content(3)), actor="owner"))
    e = only_event(live_owner, start)
    assert e["event_type"] == "memory.updated"
    assert (e["memory_old_level"], e["memory_level"]) == (None, "personal")  # level unchanged

    start = last_event_id(live_owner)
    store(lambda s: s.update(owned.id, RecordChanges(privacy="personal"), actor="owner"))
    assert only_event(live_owner, start)["memory_old_level"] is None  # same level again

    start = last_event_id(live_owner)
    store(lambda s: s.delete(proposed.id, actor="owner"))
    e = only_event(live_owner, start)
    assert e["event_type"] == "memory.deleted"
    assert (e["memory_record_id"], e["memory_category"], e["memory_source"]) == (
        str(proposed.id),
        "task",
        "model",
    )
    assert e["memory_level"] == "sensitive"

    start = last_event_id(live_owner)
    store(lambda s: s.get(owned.id))
    store(lambda s: s.list(ListFilter()))
    assert events_after(live_owner, start) == []  # reads write no event


def test_failed_operations_write_no_event(store: Run[Any], live_owner: Settings) -> None:
    made = store(lambda s: s.create(owner_record(1, key=KEY)))
    pending = store(lambda s: s.create(proposal(2, key=KEY)))
    start = last_event_id(live_owner)
    before = snapshot(live_owner)
    for attempt, error in [
        (lambda s: s.create(owner_record(3, key=KEY)), KeyConflict),
        (lambda s: s.create(_bypass(owner_record(4), privacy="secret")), InvalidRecord),
        (lambda s: s.update(made.id, RecordChanges(content="z"), actor="model"), NotAllowed),
        (lambda s: s.update(uuid4(), RecordChanges(content="z"), actor="owner"), RecordNotFound),
        (lambda s: s.approve(made.id, Approval("public"), actor="owner"), NotPending),
        (lambda s: s.approve(pending.id, Approval("public"), actor="tool"), NotAllowed),
        (lambda s: s.delete(uuid4(), actor="owner"), RecordNotFound),
        (lambda s: s.delete(made.id, actor="model"), NotAllowed),
    ]:
        with pytest.raises(error):
            store(attempt)
    assert events_after(live_owner, start) == []
    assert snapshot(live_owner) == before


class _AfterEvent(Exception):
    pass


def test_a_failure_after_the_event_rolls_both_back(
    store: Run[Any], live_owner: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The event is inserted, then the operation fails: the transaction rolls back,
    # taking the event and the change with it.
    import memory.store as store_module

    made = store(lambda s: s.create(owner_record(1)))
    from core.events import write_memory_event as real_write

    async def write_then_fail(conn: Any, event: Any) -> int:
        await real_write(conn, event)
        raise _AfterEvent

    monkeypatch.setattr(store_module, "write_memory_event", write_then_fail)
    start = last_event_id(live_owner)
    before = snapshot(live_owner)
    for attempt in (
        lambda s: s.create(owner_record(2)),
        lambda s: s.update(made.id, RecordChanges(privacy="sensitive"), actor="owner"),
        lambda s: s.delete(made.id, actor="owner"),
    ):
        with pytest.raises(_AfterEvent):
            store(attempt)
    assert events_after(live_owner, start) == []
    assert snapshot(live_owner) == before


def test_a_refused_event_rolls_the_memory_change_back(
    store: Run[Any], live_owner: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The database refuses the event (CHECK), so the whole operation is undone.
    import memory.store as store_module
    from core.events import Event

    made = store(lambda s: s.create(owner_record(1)))
    pending = store(lambda s: s.create(proposal(2)))
    from core.events import memory_record_event as real_build

    def bad_event(*args: Any, **kwargs: Any) -> Event:
        good = real_build(*args, **kwargs)
        # Skips the model's validation, so only the database CHECK can refuse it.
        return Event.model_construct(**{**dict(good), "memory_category": "operational"})

    monkeypatch.setattr(store_module, "memory_record_event", bad_event)
    start = last_event_id(live_owner)
    before = snapshot(live_owner)
    for attempt in (
        lambda s: s.create(owner_record(3)),
        lambda s: s.create(proposal(4)),
        lambda s: s.update(made.id, RecordChanges(content=content(5)), actor="owner"),
        lambda s: s.approve(pending.id, Approval("public"), actor="owner"),
        lambda s: s.delete(made.id, actor="owner"),
    ):
        with pytest.raises(InvalidRecord) as info:
            store(attempt)
        assert "events_memory_category" in str(info.value)
        assert info.value.__cause__ is None and info.value.__context__ is None
    assert events_after(live_owner, start) == []
    assert snapshot(live_owner) == before
    assert store(lambda s: s.get(made.id)).content == content(1)
