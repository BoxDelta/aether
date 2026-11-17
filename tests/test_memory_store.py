"""The memory store's rules, without a database (ADR-024 §1, §2; #69).

Everything here is refused before the store touches the engine: the engine is a
stub that fails the test if it is used. Content and keys are invented canaries;
every error message and repr is checked not to contain them.
Live behaviour is in tests/test_memory_store_live.py.
"""

import asyncio
import importlib.util
import re
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest

from memory import store as store_module
from memory.store import (
    CATEGORIES,
    CONTENT_MAX,
    KEY_PATTERN,
    UNSET,
    Approval,
    InvalidRecord,
    ListFilter,
    MemoryRecord,
    MemoryStore,
    MemoryStoreError,
    NewRecord,
    NotAllowed,
    RecordChanges,
    SecretRefused,
)
from router.contract import NON_SECRET_LEVELS, SECRET
from tests.test_privacy_levels import REPO

CANARY = "canary-memstore-41c9 the owner's invented allergy is to pineapples"
CANARY_KEY = "canary.memstore_41c9"


class _NoEngine:
    """Fails the test if the store reaches for the database."""

    def begin(self) -> Any:
        raise AssertionError("the store touched the database")


def no_db_store() -> MemoryStore:
    return MemoryStore(_NoEngine())  # type: ignore[arg-type]


def refused(make: Any, error: type[Exception] = InvalidRecord) -> MemoryStoreError:
    with pytest.raises(error) as info:
        make()
    exc = info.value
    assert isinstance(exc, MemoryStoreError)
    assert CANARY not in str(exc) and CANARY_KEY not in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    return exc


def new(**changes: Any) -> NewRecord:
    values: dict[str, Any] = {
        "content": CANARY,
        "category": "preference",
        "privacy": "personal",
        "key": CANARY_KEY,
    }
    return NewRecord(**{**values, **changes})


# --- secret is never stored ----------------------------------------------------------


def test_secret_is_refused_everywhere_a_level_is_given() -> None:
    for make in (
        lambda: new(privacy=SECRET),
        lambda: new(privacy=SECRET, source="model"),
        lambda: RecordChanges(privacy=SECRET),
        lambda: Approval(privacy=SECRET),
        lambda: ListFilter(privacy=SECRET),
    ):
        exc = refused(make, SecretRefused)
        assert "never stored" in str(exc)


def test_secret_refusal_does_not_echo_secret_shaped_content() -> None:
    content = "sk-ant-api03-FAKE-memstore-0000 my invented key"  # gitleaks:allow
    with pytest.raises(SecretRefused) as info:
        NewRecord(content=content, category="reference", privacy=SECRET)
    assert "sk-ant" not in str(info.value) and "invented" not in str(info.value)


@pytest.mark.parametrize("level", ["Secret", "unknown", "", None, 3])
def test_unknown_levels_are_refused(level: Any) -> None:
    exc = refused(lambda: new(privacy=level))
    assert exc.field == "privacy"  # type: ignore[attr-defined]


@pytest.mark.parametrize("level", NON_SECRET_LEVELS)
def test_every_non_secret_level_is_accepted(level: str) -> None:
    assert new(privacy=level).privacy == level


# --- content and key -----------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", "x" * (CONTENT_MAX + 1), CANARY + "\x00", CANARY + "\ud800", None, b"bytes"],
    ids=["empty", "too long", "NUL", "lone surrogate", "None", "bytes"],
)
def test_bad_content_is_refused(content: Any) -> None:
    exc = refused(lambda: new(content=content))
    assert exc.field == "content"  # type: ignore[attr-defined]
    refused(lambda: RecordChanges(content=content))
    refused(lambda: Approval(privacy="public", content=content))


def test_content_length_boundaries() -> None:
    assert new(content="x").content == "x"
    assert len(new(content="é" * CONTENT_MAX).content) == CONTENT_MAX  # characters, not bytes


@pytest.mark.parametrize(
    "key",
    ["", "Canary", "canary key", "canary-key", "k" * 65, "canäry", "canary\n", 5],
)
def test_bad_keys_are_refused(key: Any) -> None:
    exc = refused(lambda: new(key=key))
    assert exc.field == "key"  # type: ignore[attr-defined]
    refused(lambda: RecordChanges(key=key))
    refused(lambda: Approval(privacy="public", key=key))
    refused(lambda: ListFilter(key=key))


def test_good_keys_and_no_key() -> None:
    for key in ("a", "k" * 64, "a.b_c9", None):
        assert new(key=key).key == key


# --- other fields --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"category": "operational"}, "category"),
        ({"source": "web"}, "source"),
        ({"source_provider": "Claude API"}, "source_provider"),
        ({"source_request_id": "a b"}, "source_request_id"),
        ({"expires_at": datetime(2030, 1, 1)}, "expires_at"),  # naive on purpose
        ({"expires_at": "2030-01-01"}, "expires_at"),
    ],
)
def test_bad_fields_are_refused(changes: dict[str, Any], field: str) -> None:
    exc = refused(lambda: new(**changes))
    assert exc.field == field  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "rejected"},
        {"category": "operational"},
        {"limit": 0},
        {"limit": 201},
        {"limit": True},
        {"offset": -1},
        {"include_expired": "yes"},
    ],
)
def test_bad_list_filters_are_refused(changes: dict[str, Any]) -> None:
    refused(lambda: ListFilter(**changes))


# --- who may do what -----------------------------------------------------------------


@pytest.mark.parametrize("actor", ["model", "tool", "Owner", None])
def test_only_the_owner_may_update_approve_or_delete(actor: Any) -> None:
    store = no_db_store()
    record_id = uuid4()
    error = NotAllowed if actor in ("model", "tool") else InvalidRecord
    refused(
        lambda: asyncio.run(store.update(record_id, RecordChanges(content=CANARY), actor=actor)),
        error,
    )
    refused(lambda: asyncio.run(store.approve(record_id, Approval("public"), actor=actor)), error)
    refused(lambda: asyncio.run(store.delete(record_id, actor=actor)), error)


def test_approval_requires_a_level() -> None:
    with pytest.raises(TypeError):
        Approval()  # type: ignore[call-arg]
    refused(lambda: Approval(privacy=None))  # type: ignore[arg-type]


def test_nothing_to_change_is_refused_without_the_database() -> None:
    store = no_db_store()
    refused(lambda: asyncio.run(store.update(uuid4(), RecordChanges(), actor="owner")))


def test_ids_must_be_uuids() -> None:
    store = no_db_store()
    refused(lambda: asyncio.run(store.get("not-a-uuid")))  # type: ignore[arg-type]
    refused(lambda: asyncio.run(store.delete(str(uuid4()), actor="owner")))  # type: ignore[arg-type]


def test_wrong_input_types_are_refused() -> None:
    store = no_db_store()
    refused(lambda: asyncio.run(store.create({"content": CANARY})))  # type: ignore[arg-type]
    refused(lambda: asyncio.run(store.list({"key": CANARY_KEY})))  # type: ignore[arg-type]


def test_changes_list_only_the_fields_given() -> None:
    assert RecordChanges().columns() == {}
    assert RecordChanges(key=None, privacy="public").columns() == {"key": None, "privacy": "public"}
    assert Approval("sensitive").columns() == {"privacy": "sensitive"}
    assert Approval("public", category="task").columns() == {
        "category": "task",
        "privacy": "public",
    }
    assert RecordChanges().content is UNSET


# --- content and key stay out of reprs -----------------------------------------------


def test_reprs_hold_no_content_or_key() -> None:
    record = MemoryRecord(
        id=UUID(int=1),
        content=CANARY,
        key=CANARY_KEY,
        category="preference",
        privacy="personal",
        status="approved",
        source="owner",
        source_provider=None,
        source_request_id=None,
        privacy_source="owner",
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
        updated_at=datetime(2026, 11, 17, tzinfo=UTC),
        approved_at=datetime(2026, 11, 17, tzinfo=UTC),
        expires_at=None,
    )
    for obj in (
        record,
        new(),
        RecordChanges(content=CANARY, key=CANARY_KEY),
        Approval("public", content=CANARY, key=CANARY_KEY),
        ListFilter(key=CANARY_KEY),
    ):
        for text in (repr(obj), str(obj)):
            assert CANARY not in text and CANARY_KEY not in text


def test_a_record_read_back_as_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        MemoryRecord.model_validate(
            {**_record_fields(), "privacy": SECRET}  # the database CHECK forbids this anyway
        )


def _record_fields() -> dict[str, Any]:
    now = datetime(2026, 11, 17, tzinfo=UTC)
    return {
        "id": UUID(int=2),
        "content": "x",
        "key": None,
        "category": "task",
        "privacy": "public",
        "status": "pending",
        "source": "model",
        "source_provider": None,
        "source_request_id": None,
        "privacy_source": "suggested",
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "expires_at": None,
    }


# --- the store's lists match the frozen copies in migration 0004 ---------------------


def _migration() -> ModuleType:
    path = REPO / "migrations/versions/0004_memory_records.py"
    spec = importlib.util.spec_from_file_location("m0004", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_store_matches_migration_0004() -> None:
    migration = _migration()
    assert f"public.{migration.TABLE}" == store_module.TABLE
    assert CONTENT_MAX == migration.CONTENT_MAX
    assert CATEGORIES == migration.CATEGORIES
    assert store_module.STATUSES == migration.STATUSES
    assert store_module.SOURCES == migration.SOURCES
    assert get_args(store_module.PrivacySource) == migration.PRIVACY_SOURCES
    source = (REPO / "migrations/versions/0004_memory_records.py").read_text()
    assert f"key ~ '^{KEY_PATTERN}$'" in source
    assert set(MemoryRecord.model_fields) == set(re.findall(r'sa\.Column\(\s*"(\w+)"', source))
