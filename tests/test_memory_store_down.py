"""The memory store with the database down (#80; ADR-026 §5, proposed in #82).

With the encrypted volume locked, Postgres is not running. Every operation must
then raise ``StoreUnavailable``: never an empty list, ``RecordNotFound`` or a write
that silently did nothing. The error names the error type only, and nothing is
chained (the driver's exception can carry the host or the row).
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError

from core.config import Settings
from core.db import create_engine
from core.log import SecretCheck
from memory.store import (
    Approval,
    ListFilter,
    MemoryStore,
    NewRecord,
    RecordChanges,
    StoreFailed,
    StoreUnavailable,
)
from tests.fake_secrets import configured_value

PASSWORD = "fake-pw-memdown-" + configured_value()
# Valid URL, nothing listening on port 1: connecting is refused at once.
DOWN_URL = f"postgresql+asyncpg://aether_app:{PASSWORD}@127.0.0.1:1/aether"
CANARY = "canary-memdown-2f60 an invented fact about the owner's bicycle"


def new() -> NewRecord:
    return NewRecord(content=CANARY, category="preference", privacy="public", source="owner")


def operations(store: MemoryStore) -> dict[str, Any]:
    record_id = uuid4()
    return {
        "create": lambda: store.create(new()),
        "get": lambda: store.get(record_id),
        "list": lambda: store.list(),
        "list pending": lambda: store.list(ListFilter(status="pending")),
        "search": lambda: store.list(ListFilter(query="bicycle")),
        "update": lambda: store.update(record_id, RecordChanges(content=CANARY), actor="owner"),
        "approve": lambda: store.approve(record_id, Approval("public"), actor="owner"),
        "delete": lambda: store.delete(record_id, actor="owner"),
    }


def assert_unavailable(exc: BaseException) -> None:
    assert isinstance(exc, StoreUnavailable) and isinstance(exc, StoreFailed)
    assert PASSWORD not in str(exc) and CANARY not in str(exc)
    assert "127.0.0.1" not in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None


@pytest.mark.parametrize("name", list(operations(MemoryStore.__new__(MemoryStore))))
def test_every_operation_is_unavailable_when_postgres_is_down(name: str) -> None:
    settings = Settings(_env_file=None, database_url=SecretStr(DOWN_URL))

    async def run() -> BaseException:
        engine = create_engine(settings)
        try:
            store = MemoryStore(engine, SecretCheck.from_settings(settings))
            with pytest.raises(StoreUnavailable) as info:
                await operations(store)[name]()
            return info.value
        finally:
            await engine.dispose()

    assert_unavailable(asyncio.run(run()))


# --- a connection lost during an operation -------------------------------------------


class _Conn:
    """Connects fine, then the given error happens inside the transaction."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.closed = False

    async def start(self) -> "_Conn":
        return self

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def close(self) -> None:
        self.closed = True
        raise OSError("closing a dead connection fails too")


class _Engine:
    def __init__(self, error: BaseException) -> None:
        self.conn = _Conn(error)

    def connect(self) -> _Conn:
        return self.conn


def _lost(error: BaseException) -> tuple[BaseException, _Engine]:
    engine = _Engine(error)
    store = MemoryStore(engine, SecretCheck())  # type: ignore[arg-type]
    with pytest.raises(StoreFailed) as info:
        asyncio.run(store.list())
    return info.value, engine


def test_a_connection_lost_mid_operation_is_unavailable() -> None:
    orig = ConnectionResetError(f"reset by peer at 127.0.0.1 {PASSWORD}")
    error = DBAPIError("SELECT ...", None, orig, connection_invalidated=True)
    exc, engine = _lost(error)
    assert_unavailable(exc)
    assert engine.conn.closed


@pytest.mark.parametrize("error", [OSError("broken pipe"), TimeoutError()])
def test_a_network_error_mid_operation_is_unavailable(error: BaseException) -> None:
    exc, _ = _lost(error)
    assert_unavailable(exc)


def test_other_database_errors_are_not_reported_as_unavailable() -> None:
    # A database that answers with an error is up: a plain StoreFailed (500), so the
    # owner isn't told to check the volume when the problem is elsewhere.
    orig = RuntimeError(f"permission denied {CANARY}")
    exc, _ = _lost(DBAPIError("SELECT ...", None, orig, connection_invalidated=False))
    assert type(exc) is StoreFailed
    assert CANARY not in str(exc) and exc.__cause__ is None and exc.__context__ is None
