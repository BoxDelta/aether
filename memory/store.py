"""The memory store (ADR-024 §1, §2, §4; step 4 of #58): ``public.memory_records``.

A Python API only: no HTTP route and no retrieval for model calls yet (ADR-024 §9,
steps 6 and 11). It uses Core's app engine (``core.db.create_engine``,
the ``aether_app`` role) and changes nothing about its privileges.

Rules, checked here before anything reaches the database; the CHECK constraints of
migration 0004 are the backstop:

- **Privacy** comes from ``router.contract.PrivacyLevel``. ``secret`` is never stored
  (docs/05 §7.1): it is refused with ``SecretRefused``.
- **Who creates.** A record from the owner is ``approved`` at once, with the level
  the owner gave (``privacy_source = owner``, Decision D1). A record from a model
  or a tool is always ``pending`` and its level is only a suggestion
  (``privacy_source = suggested``). The store never takes "approved" from a caller.
- **Only the owner** may update, approve or delete (``NotAllowed`` otherwise):
  a model or tool can never approve, change or delete anything (ADR-024 §2).
  The store can't tell who is calling: it trusts the ``source`` and ``actor`` its
  caller passes, and ``source`` has no default. Callers (the API from step 6 on)
  must set them from authentication (ADR-021), never from a request body or model
  output.
- **Approve**: the owner states the final level (it is required), which sets
  ``approved_at`` and ``privacy_source = owner``; content, category and key may be
  corrected in the same step. Any level the owner sets with ``update`` is also
  recorded as the owner's choice (``privacy_source = owner``).
- **Content** 1 to ``CONTENT_MAX`` characters of valid text; **key** matches
  ``KEY_PATTERN``; at most one approved record per key, so a clash raises
  ``KeyConflict`` (approve does not yet replace the old record: step 8, #67).
- **Expiry**: a record past ``expires_at`` is left out of ``get`` and ``list`` unless
  asked for, and can't be updated or approved; it can still be deleted. Nothing
  deletes expired records yet (the clean-up is step 8).
- **Delete** is a hard delete: the row is gone, no tombstone (ADR-024 §4).

What never leaves the store: memory content and keys never appear in log records,
error messages or reprs. Errors name the field and the rule, never the value.
Database errors are turned into store errors raised *outside* the ``except``
block, so the driver's exception (whose detail can quote the row) is not even
chained. Logs carry the record ID and status only.

Every operation runs in one transaction: if it fails, nothing is changed.

**Events** (ADR-024 §5, step 5): every change writes exactly one metadata-only event
in the *same* transaction, with actor ``memory``, so the change and its event commit
or roll back together. A failed operation writes no event, and a failed event write
undoes the change:

- create by a model or a tool (``pending``): ``memory.proposed``;
- create by the owner (approved at once, D1) and ``approve``: ``memory.approved``;
- ``update``: ``memory.updated``, with the old level as well when the level changed;
- ``delete``: ``memory.deleted``.

An event holds the record's ID, category, source and level (``MemoryRecordMeta``),
never content, a key or corrected text. Reads (``get``, ``list``) write no event.
"""

import enum
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.events import (
    MemoryRecordEventType,
    MemoryRecordMeta,
    memory_record_event,
    write_memory_event,
)
from core.request_context import SAFE_ID_PATTERN
from memory.types import (
    CATEGORIES,
    SOURCES,
    STATUSES,
    Category,
    PrivacySource,
    Source,
    Status,
)
from router.contract import (
    NON_SECRET_LEVELS,
    PROVIDER_NAME_PATTERN,
    SECRET,
    NonSecretLevel,
    PrivacyLevel,
)

logger = logging.getLogger("aether.memory.store")

# Frozen copies of these (and of memory/types.py) live in
# migrations/versions/0004_memory_records.py; tests/test_memory_store.py fails if
# they drift apart.
TABLE = "public.memory_records"  # schema-qualified, like Core's privilege check (#67)
CONTENT_MAX = 1000
KEY_PATTERN = r"[a-z0-9_.]{1,64}"
OWNER: Source = "owner"
LIST_LIMIT_MAX = 200
LIST_LIMIT_DEFAULT = 50

_KEY = re.compile(KEY_PATTERN)
_PROVIDER = re.compile(PROVIDER_NAME_PATTERN)
_REQUEST_ID = re.compile(SAFE_ID_PATTERN)
_UNIQUE_VIOLATION = "23505"
_CHECK_VIOLATION = "23514"
_KEY_INDEX = "memory_records_approved_key"


# --- errors: field and rule only, never the value -----------------------------------


class MemoryStoreError(Exception):
    """Base of every store error. Messages never contain content or keys."""


class InvalidRecord(MemoryStoreError):
    def __init__(self, field_name: str, rule: str) -> None:
        super().__init__(f"{field_name}: {rule}")
        self.field = field_name
        self.rule = rule


class SecretRefused(InvalidRecord):
    def __init__(self) -> None:
        super().__init__("privacy", f"'{SECRET}' is never stored in memory (docs/05 §7.1)")


class NotAllowed(MemoryStoreError):
    """A model or a tool tried something only the owner may do."""


class RecordNotFound(MemoryStoreError):
    def __init__(self, record_id: UUID) -> None:
        super().__init__(f"memory record {record_id} not found")
        self.record_id = record_id


class NotPending(MemoryStoreError):
    def __init__(self, record_id: UUID) -> None:
        super().__init__(f"memory record {record_id} is not pending")
        self.record_id = record_id


class KeyConflict(MemoryStoreError):
    def __init__(self) -> None:
        super().__init__("another approved memory record already uses this key")


class StoreFailed(MemoryStoreError):
    """Any other database error, described by its SQLSTATE or type name only."""


# --- checks ----------------------------------------------------------------------------


def _check_content(value: object) -> None:
    if not isinstance(value, str):
        raise InvalidRecord("content", "must be text")
    if not 1 <= len(value) <= CONTENT_MAX:
        raise InvalidRecord("content", f"must be 1 to {CONTENT_MAX} characters")
    if "\x00" in value:  # Postgres text can't hold NUL
        raise InvalidRecord("content", "must not contain NUL characters")
    if not value.isascii() and not _encodes(value):
        raise InvalidRecord("content", "must be valid Unicode text")


def _encodes(value: str) -> bool:
    # A lone surrogate can't be sent; the encode error would carry the whole string,
    # so it is caught here and never raised or chained.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_key(value: object) -> None:
    if value is not None and not (isinstance(value, str) and _KEY.fullmatch(value)):
        raise InvalidRecord("key", "must be 1 to 64 of a-z, 0-9, '_' and '.'")


def _check_level(value: object) -> None:
    if value == SECRET:
        raise SecretRefused
    if value not in NON_SECRET_LEVELS:
        raise InvalidRecord("privacy", f"must be one of {', '.join(NON_SECRET_LEVELS)}")


def _check_choice(name: str, value: object, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise InvalidRecord(name, f"must be one of {', '.join(allowed)}")


def _check_pattern(name: str, value: object, pattern: re.Pattern[str]) -> None:
    if value is not None and not (isinstance(value, str) and pattern.fullmatch(value)):
        raise InvalidRecord(name, f"must match {pattern.pattern}")


def _check_expiry(value: object) -> None:
    if value is not None and not (isinstance(value, datetime) and value.tzinfo is not None):
        raise InvalidRecord("expires_at", "must be a timezone-aware datetime or None")


def _check_id(value: object) -> None:
    if not isinstance(value, UUID):
        raise InvalidRecord("id", "must be a UUID")


def _require_owner(actor: object, action: str) -> None:
    _check_choice("actor", actor, SOURCES)
    if actor != OWNER:
        raise NotAllowed(f"only the owner may {action} memory records")


# --- inputs ---------------------------------------------------------------------------


class _Unset(enum.Enum):
    UNSET = "UNSET"


UNSET = _Unset.UNSET  # "leave this field as it is", distinct from None


@dataclass(frozen=True, slots=True)
class NewRecord:
    """A record to create. ``privacy`` is final for the owner and a suggestion for a
    model or a tool. Validated on construction; content and key stay out of repr."""

    content: str = field(repr=False)
    category: Category
    privacy: PrivacyLevel
    # Required, no default: a forgotten source must never mean "the owner, approved".
    source: Source
    key: str | None = field(default=None, repr=False)
    source_provider: str | None = None
    source_request_id: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _check_level(self.privacy)
        _check_content(self.content)
        _check_key(self.key)
        _check_choice("category", self.category, CATEGORIES)
        _check_choice("source", self.source, SOURCES)
        _check_pattern("source_provider", self.source_provider, _PROVIDER)
        _check_pattern("source_request_id", self.source_request_id, _REQUEST_ID)
        _check_expiry(self.expires_at)


@dataclass(frozen=True, slots=True)
class RecordChanges:
    """An owner's correction. Fields left ``UNSET`` don't change; ``key`` and
    ``expires_at`` may be set to None to clear them (Decision D6: overwrite)."""

    content: str | _Unset = field(default=UNSET, repr=False)
    category: Category | _Unset = UNSET
    key: str | _Unset | None = field(default=UNSET, repr=False)
    privacy: PrivacyLevel | _Unset = UNSET
    expires_at: datetime | _Unset | None = UNSET

    def __post_init__(self) -> None:
        _check_changes(self.content, self.category, self.key, self.privacy)
        if self.expires_at is not UNSET:
            _check_expiry(self.expires_at)

    def columns(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in ("content", "category", "key", "privacy", "expires_at")
            if getattr(self, name) is not UNSET
        }


@dataclass(frozen=True, slots=True)
class Approval:
    """The owner approves a pending record. ``privacy`` is required: no record is
    approved with a level nobody chose (ADR-024 §1). Content, category and key may
    be corrected in the same step."""

    privacy: PrivacyLevel
    content: str | _Unset = field(default=UNSET, repr=False)
    category: Category | _Unset = UNSET
    key: str | _Unset | None = field(default=UNSET, repr=False)

    def __post_init__(self) -> None:
        _check_changes(self.content, self.category, self.key, self.privacy)

    def columns(self) -> dict[str, object]:
        found = {
            name: getattr(self, name)
            for name in ("content", "category", "key")
            if getattr(self, name) is not UNSET
        }
        return {**found, "privacy": self.privacy}


def _check_changes(content: object, category: object, key: object, privacy: object) -> None:
    if privacy is not UNSET:
        _check_level(privacy)
    if content is not UNSET:
        _check_content(content)
    if key is not UNSET:
        _check_key(key)
    if category is not UNSET:
        _check_choice("category", category, CATEGORIES)


@dataclass(frozen=True, slots=True)
class ListFilter:
    """Filters for ``MemoryStore.list``. ``key`` is content: callers must not put it
    in a URL or a log (ADR-024 §4)."""

    status: Status | None = None
    privacy: PrivacyLevel | None = None
    category: Category | None = None
    key: str | None = field(default=None, repr=False)
    include_expired: bool = False
    limit: int = LIST_LIMIT_DEFAULT
    offset: int = 0

    def __post_init__(self) -> None:
        if self.status is not None:
            _check_choice("status", self.status, STATUSES)
        if self.privacy is not None:
            _check_level(self.privacy)
        if self.category is not None:
            _check_choice("category", self.category, CATEGORIES)
        _check_key(self.key)
        if not isinstance(self.include_expired, bool):
            raise InvalidRecord("include_expired", "must be true or false")
        if not _is_int(self.limit) or not 1 <= self.limit <= LIST_LIMIT_MAX:
            raise InvalidRecord("limit", f"must be 1 to {LIST_LIMIT_MAX}")
        if not _is_int(self.offset) or self.offset < 0:
            raise InvalidRecord("offset", "must be 0 or more")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- the result -----------------------------------------------------------------------


class MemoryRecord(BaseModel):
    """One stored record. Content and key are left out of repr and str."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    content: str = Field(repr=False)
    key: str | None = Field(repr=False)
    category: Category
    privacy: NonSecretLevel
    status: Status
    source: Source
    source_provider: str | None
    source_request_id: str | None
    privacy_source: PrivacySource
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    expires_at: datetime | None


_COLUMNS = ", ".join(MemoryRecord.model_fields)
_NOT_EXPIRED = "(expires_at IS NULL OR expires_at > now())"
T = TypeVar("T")


def _record(row: Any) -> MemoryRecord:
    return MemoryRecord.model_validate(dict(row._mapping))


def _meta(record: MemoryRecord) -> MemoryRecordMeta:
    return MemoryRecordMeta(
        record_id=record.id, category=record.category, source=record.source, level=record.privacy
    )


def _failure(exc: DBAPIError) -> MemoryStoreError:
    """A store error for a database error, from its SQLSTATE and constraint name
    only. Never the driver's message or detail, which can quote the row."""
    orig = exc.orig
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    if state == _UNIQUE_VIOLATION and constraint == _KEY_INDEX:
        return KeyConflict()
    if state == _CHECK_VIOLATION and isinstance(constraint, str):
        return InvalidRecord("record", f"refused by database check {constraint}")
    return StoreFailed(f"database error ({state or type(orig).__name__})")


# --- the store ------------------------------------------------------------------------


class MemoryStore:
    """Create, read, correct, approve and hard-delete memory records.

    ``engine`` is Core's app engine (``core.db.create_engine``); the store never
    disposes it.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _run(self, operation: Callable[[AsyncConnection], Awaitable[T]]) -> T:
        """Run ``operation`` in one transaction (rolled back if it raises)."""
        failure: MemoryStoreError
        try:
            async with self._engine.begin() as conn:
                return await operation(conn)
        except DBAPIError as exc:
            failure = _failure(exc)
        except (StatementError, ValidationError) as exc:
            # Not a database refusal: a statement that couldn't be built or a row that
            # doesn't fit MemoryRecord. Type name only.
            failure = StoreFailed(f"store error ({type(exc).__name__})")
        # Raised here, outside the except block, so the driver error isn't chained.
        raise failure

    async def create(self, new: NewRecord) -> MemoryRecord:
        if not isinstance(new, NewRecord):
            raise InvalidRecord("record", "must be a NewRecord")
        owner = new.source == OWNER
        values = {
            "content": new.content,
            "key": new.key,
            "category": new.category,
            "privacy": new.privacy,
            "status": "approved" if owner else "pending",
            "source": new.source,
            "source_provider": new.source_provider,
            "source_request_id": new.source_request_id,
            "privacy_source": "owner" if owner else "suggested",
            "expires_at": new.expires_at,
        }
        approved_at = "now()" if owner else "NULL"
        sql = (
            f"INSERT INTO {TABLE} ({', '.join(values)}, approved_at)"  # noqa: S608 - fixed names
            f" VALUES ({', '.join(':' + c for c in values)}, {approved_at})"
            f" RETURNING {_COLUMNS}"
        )

        async def insert(conn: AsyncConnection) -> MemoryRecord:
            record = _record((await conn.execute(text(sql), values)).one())
            kind: MemoryRecordEventType = (
                "memory.approved" if record.status == "approved" else "memory.proposed"
            )
            await write_memory_event(conn, memory_record_event(kind, _meta(record)))
            return record

        record = await self._run(insert)
        logger.info(
            "Memory record created", extra={"record_id": str(record.id), "status": record.status}
        )
        return record

    async def get(self, record_id: UUID, *, include_expired: bool = False) -> MemoryRecord:
        _check_id(record_id)
        expiry = "" if include_expired else f" AND {_NOT_EXPIRED}"
        sql = f"SELECT {_COLUMNS} FROM {TABLE} WHERE id = :id{expiry}"  # noqa: S608

        async def select(conn: AsyncConnection) -> MemoryRecord:
            row = (await conn.execute(text(sql), {"id": record_id})).one_or_none()
            if row is None:
                raise RecordNotFound(record_id)
            return _record(row)

        return await self._run(select)

    async def list(self, filters: ListFilter | None = None) -> list[MemoryRecord]:
        """Records matching ``filters``, most recently updated first."""
        filters = ListFilter() if filters is None else filters
        if not isinstance(filters, ListFilter):
            raise InvalidRecord("filters", "must be a ListFilter")
        conditions = [] if filters.include_expired else [_NOT_EXPIRED]
        params: dict[str, object] = {"limit": filters.limit, "offset": filters.offset}
        for name in ("status", "privacy", "category", "key"):
            value = getattr(filters, name)
            if value is not None:
                conditions.append(f"{name} = :{name}")
                params[name] = value
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT {_COLUMNS} FROM {TABLE}{where}"  # noqa: S608 - fixed names only
            " ORDER BY updated_at DESC, id LIMIT :limit OFFSET :offset"
        )

        async def select(conn: AsyncConnection) -> list[MemoryRecord]:
            return [_record(row) for row in await conn.execute(text(sql), params)]

        return await self._run(select)

    async def update(
        self, record_id: UUID, changes: RecordChanges, *, actor: Source
    ) -> MemoryRecord:
        """Correct a record (owner only). A level set here is the owner's choice."""
        _require_owner(actor, "update")
        _check_id(record_id)
        if not isinstance(changes, RecordChanges):
            raise InvalidRecord("changes", "must be a RecordChanges")
        columns = changes.columns()
        if not columns:
            raise InvalidRecord("changes", "nothing to change")
        extra = ", privacy_source = 'owner'" if "privacy" in columns else ""

        async def change(conn: AsyncConnection) -> MemoryRecord:
            _, old_level = await self._lock(conn, record_id)
            record = await self._set(conn, record_id, columns, extra)
            changed = old_level if old_level != record.privacy else None
            event = memory_record_event("memory.updated", _meta(record), old_level=changed)
            await write_memory_event(conn, event)
            return record

        record = await self._run(change)
        logger.info(
            "Memory record updated",
            extra={
                "record_id": str(record.id),
                "status": record.status,
                "fields_changed": len(columns),
            },
        )
        return record

    async def approve(self, record_id: UUID, approval: Approval, *, actor: Source) -> MemoryRecord:
        """Approve a pending record with the owner's final level (owner only)."""
        _require_owner(actor, "approve")
        _check_id(record_id)
        if not isinstance(approval, Approval):
            raise InvalidRecord("approval", "must be an Approval")
        columns = approval.columns()
        extra = ", status = 'approved', privacy_source = 'owner', approved_at = now()"

        async def change(conn: AsyncConnection) -> MemoryRecord:
            status, _ = await self._lock(conn, record_id)
            if status != "pending":
                raise NotPending(record_id)
            record = await self._set(conn, record_id, columns, extra)
            await write_memory_event(conn, memory_record_event("memory.approved", _meta(record)))
            return record

        record = await self._run(change)
        logger.info(
            "Memory record approved", extra={"record_id": str(record.id), "status": record.status}
        )
        return record

    async def delete(self, record_id: UUID, *, actor: Source) -> None:
        """Hard delete (owner only), expired or not. No tombstone (ADR-024 §4)."""
        _require_owner(actor, "delete")
        _check_id(record_id)
        # Metadata columns only: the deleted content is never read back.
        sql = f"DELETE FROM {TABLE} WHERE id = :id RETURNING id, category, source, privacy"  # noqa: S608

        async def remove(conn: AsyncConnection) -> None:
            row = (await conn.execute(text(sql), {"id": record_id})).one_or_none()
            if row is None:
                raise RecordNotFound(record_id)
            meta = MemoryRecordMeta(
                record_id=row.id, category=row.category, source=row.source, level=row.privacy
            )
            await write_memory_event(conn, memory_record_event("memory.deleted", meta))

        await self._run(remove)
        logger.info("Memory record deleted", extra={"record_id": str(record_id)})

    @staticmethod
    async def _lock(conn: AsyncConnection, record_id: UUID) -> tuple[str, PrivacyLevel]:
        """Lock an unexpired record for this transaction; its status and level."""
        sql = f"SELECT status, privacy FROM {TABLE} WHERE id = :id AND {_NOT_EXPIRED} FOR UPDATE"  # noqa: S608
        row = (await conn.execute(text(sql), {"id": record_id})).one_or_none()
        if row is None:
            raise RecordNotFound(record_id)
        return str(row.status), cast(PrivacyLevel, row.privacy)

    @staticmethod
    async def _set(
        conn: AsyncConnection, record_id: UUID, columns: dict[str, object], extra: str
    ) -> MemoryRecord:
        assignments = ", ".join(f"{name} = :{name}" for name in columns)
        sql = (
            f"UPDATE {TABLE} SET {assignments}, updated_at = now(){extra}"  # noqa: S608
            f" WHERE id = :id RETURNING {_COLUMNS}"
        )
        return _record((await conn.execute(text(sql), {**columns, "id": record_id})).one())
