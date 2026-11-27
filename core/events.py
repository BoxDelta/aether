"""Append-only events: metadata about what AETHER did, never content (#21, docs/01 §7).

An event records timestamps, request/session IDs, privacy level, the route
decision and its reason, failed attempts (provider, error type, scrubbed
message), latency and token counts. It never records prompt text, answer text,
memory content, secrets or tracebacks:

- ``Event`` has typed fields only, mirrored by CHECK constraints in the table
  (migration 0002); there is no free-form field for content to land in.
- Builders take metadata objects (``RouteDecision``, ``ModelResponse``,
  ``Attempt``, ``RouteFailed``), never a request's messages, and copy only
  ``ProviderError.message`` for errors, never ``__cause__``/``__context__``.
- Memory events (ADR-024 §5, migration 0005) carry the record's ID, category,
  source and level, or counts and record IDs, and never content, keys, corrected
  text, search queries or rejection reasons. ``write_memory_event`` writes one on
  the caller's connection, so it commits or rolls back with the change it records.
- On write, every free-text field (reason, error message, attempt messages)
  goes through the log ``Redactor`` (configured secrets, key and URL-credential
  patterns), and is blanked if it contains any of the request's own text or the
  memory text sent in its calls (backstop against an adapter echoing the prompt;
  ``router/echo.py``). Only then is it cut to ``MAX_TEXT`` characters, so a
  configured secret that straddles the cut is still masked whole (#115).
- ``memory.context.retrieved`` (``memory_retrieved``): one per call that could get
  memory, with the provider, counts per level, characters, the effective level of
  the call, the record IDs and how many were left out. Never content.
"""

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    insert,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.log import Redactor
from core.request_context import SafeId, get_request_id, get_session_id
from memory.types import Category, Source
from router.contract import (
    NON_SECRET_LEVELS,
    FinishReason,
    ModelRequest,
    ModelResponse,
    NonSecretLevel,
    PrivacyLevel,
    ProviderKind,
    ProviderName,
    privacy_rank,
)

# The echo backstop's constants and request_texts live in router/echo.py; re-exported.
from router.echo import MIN_ECHO_LENGTH as MIN_ECHO_LENGTH
from router.echo import REMOVED_ECHO as REMOVED_ECHO
from router.echo import long_enough
from router.echo import request_texts as request_texts
from router.routing import Attempt, MemoryUse, NoRoute, RouteDecision, RouteFailed, RouteOptions

logger = logging.getLogger("aether.core.events")

SCHEMA_VERSION = 1
# One request's events are written in one transaction; a slow or unreachable
# database must not hold anything up for longer than this.
WRITE_TIMEOUT_S = 3.0
MAX_TEXT = 500

MemoryRecordEventType = Literal[
    "memory.proposed",
    "memory.approved",
    "memory.rejected",
    "memory.updated",
    "memory.deleted",
    "memory.expired",
]
MemoryEventType = Literal[
    MemoryRecordEventType,
    "memory.exported",
    "memory.context.retrieved",
]
EventType = Literal[
    "request.received",
    "model.route.selected",
    "model.call.completed",
    "provider.unavailable",
    "request.failed",
    MemoryEventType,
]
Actor = Literal["core", "router", "memory", "restore"]
MemoryActor = Literal["memory", "restore"]
MEMORY_RECORD_EVENT_TYPES: tuple[MemoryRecordEventType, ...] = get_args(MemoryRecordEventType)
MEMORY_EVENT_TYPES: tuple[str, ...] = (
    *MEMORY_RECORD_EVENT_TYPES,
    "memory.exported",
    "memory.context.retrieved",
)
MEMORY_ACTORS: tuple[MemoryActor, ...] = get_args(MemoryActor)
# At most this many record IDs per memory.context.retrieved event (ADR-024 §3, D9).
MAX_MEMORY_RECORD_IDS = 20
Outcome = Literal["ok", "refusal", "error"]

ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:/-]{1,200}$")]
TypeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
# Free text (route reasons, provider error messages) as built, not yet stored:
# uncapped, because a cap before redaction can cut a configured secret into a prefix
# that no longer matches by value (#115). ``to_row`` redacts, echo-checks, then caps
# at MAX_TEXT; the database CHECK on the columns is the backstop.
RawText = str
NonNegInt = Annotated[int, Field(ge=0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventAttempt(_Frozen):
    provider: ProviderName
    error_type: TypeName
    message: RawText
    retryable: bool


class Event(_Frozen):
    event_type: EventType
    actor: Actor
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: Annotated[int, Field(ge=1)] = SCHEMA_VERSION
    outcome: Outcome | None = None
    request_id: SafeId | None = None
    session_id: SafeId | None = None
    privacy: PrivacyLevel | None = None
    allow_cloud: bool | None = None
    include_sensitive: bool | None = None  # migration 0008 (#144)
    override: ProviderName | None = None
    provider: ProviderName | None = None
    provider_kind: ProviderKind | None = None
    model: ModelName | None = None
    route_reason: RawText | None = None
    finish_reason: FinishReason | None = None
    latency_ms: Annotated[float, Field(ge=0.0)] | None = None
    input_tokens: NonNegInt | None = None
    output_tokens: NonNegInt | None = None
    error_type: TypeName | None = None
    error_message: RawText | None = None
    attempts: Annotated[tuple[EventAttempt, ...], Field(max_length=2)] | None = None
    # Memory events (ADR-024 §5, migration 0005). Never content, keys or queries.
    memory_record_id: UUID | None = None
    memory_category: Category | None = None
    memory_source: Source | None = None
    memory_level: NonSecretLevel | None = None
    memory_old_level: NonSecretLevel | None = None
    # One count per non-secret level, in router.contract order (tests check this).
    memory_count_public: NonNegInt | None = None
    memory_count_personal: NonNegInt | None = None
    memory_count_sensitive: NonNegInt | None = None
    memory_chars: NonNegInt | None = None
    memory_effective_level: NonSecretLevel | None = None
    memory_record_ids: (
        Annotated[tuple[UUID, ...], Field(max_length=MAX_MEMORY_RECORD_IDS)] | None
    ) = None
    memory_left_out: NonNegInt | None = None
    memory_export_count: NonNegInt | None = None

    @model_validator(mode="after")
    def _memory_shape(self) -> Self:
        """The same rules as migration 0005's CHECKs, so a bad event fails before
        it reaches the database."""
        _check_memory_shape(self)
        return self


MEMORY_RECORD_FIELDS = ("memory_record_id", "memory_category", "memory_source", "memory_level")
MEMORY_COUNT_FIELDS = tuple(f"memory_count_{level}" for level in NON_SECRET_LEVELS)
MEMORY_RETRIEVED_FIELDS = (
    *MEMORY_COUNT_FIELDS,
    "memory_chars",
    "memory_effective_level",
    "memory_record_ids",
    "memory_left_out",
)


def _set(event: Event, names: Sequence[str]) -> int:
    return sum(getattr(event, n) is not None for n in names)


def _check_retrieved(event: Event) -> None:
    counts = [getattr(event, n) for n in MEMORY_COUNT_FIELDS]
    if event.request_id is None or event.provider is None:
        raise ValueError("memory.context.retrieved needs a request id and a provider")
    ids = event.memory_record_ids or ()
    if len(ids) != sum(counts) or len(set(ids)) != len(ids):
        raise ValueError("one distinct record id per record sent")
    effective = privacy_rank(event.memory_effective_level)  # type: ignore[arg-type]
    if any(
        c and privacy_rank(level) > effective
        for level, c in zip(NON_SECRET_LEVELS, counts, strict=True)
    ):
        raise ValueError("effective level below a level that was sent")


def _check_memory_shape(event: Event) -> None:
    kind = event.event_type
    is_memory = kind in MEMORY_EVENT_TYPES
    if is_memory != (event.actor in MEMORY_ACTORS):
        raise ValueError("memory events have actor memory or restore, and only they do")
    record = kind in MEMORY_RECORD_EVENT_TYPES
    if _set(event, MEMORY_RECORD_FIELDS) != (len(MEMORY_RECORD_FIELDS) if record else 0):
        raise ValueError("record id, category, source and level: all on record events, else none")
    if event.memory_old_level is not None and (
        kind != "memory.updated" or event.memory_old_level == event.memory_level
    ):
        raise ValueError("old level only on memory.updated, and only when the level changed")
    retrieved = kind == "memory.context.retrieved"
    if _set(event, MEMORY_RETRIEVED_FIELDS) != (len(MEMORY_RETRIEVED_FIELDS) if retrieved else 0):
        raise ValueError("context fields: all on memory.context.retrieved, else none")
    if retrieved:
        _check_retrieved(event)
    if (event.memory_export_count is not None) != (kind == "memory.exported"):
        raise ValueError("export count on memory.exported only")
    if is_memory and (event.route_reason, event.error_message, event.attempts) != (None,) * 3:
        raise ValueError("memory events carry no free text")
    if is_memory and event.include_sensitive is not None:
        raise ValueError("memory events don't carry include_sensitive (migration 0008)")


# SQLAlchemy Core view of the table created by migration 0002 (used for inserts).
metadata = MetaData()
events_table = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("event_type", Text, nullable=False),
    Column("schema_version", SmallInteger, nullable=False),
    Column("actor", Text, nullable=False),
    Column("outcome", Text),
    Column("request_id", Text),
    Column("session_id", Text),
    Column("privacy", Text),
    Column("allow_cloud", Boolean),
    Column("include_sensitive", Boolean),  # migration 0008
    Column("override", Text),
    Column("provider", Text),
    Column("provider_kind", Text),
    Column("model", Text),
    Column("route_reason", Text),
    Column("finish_reason", Text),
    Column("latency_ms", Double),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("error_type", Text),
    Column("error_message", Text),
    # none_as_null: Python None must be SQL NULL, not JSON null (CHECK requires an array).
    Column("attempts", JSONB(none_as_null=True)),
    # Migration 0005.
    Column("memory_record_id", PgUUID(as_uuid=True)),
    Column("memory_category", Text),
    Column("memory_source", Text),
    Column("memory_level", Text),
    Column("memory_old_level", Text),
    Column("memory_effective_level", Text),
    Column("memory_count_public", Integer),
    Column("memory_count_personal", Integer),
    Column("memory_count_sensitive", Integer),
    Column("memory_chars", Integer),
    Column("memory_left_out", Integer),
    Column("memory_export_count", Integer),
    Column("memory_record_ids", ARRAY(PgUUID(as_uuid=True))),
)


# --- builders ---------------------------------------------------------------------


def _cap(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def request_received(request: ModelRequest, options: RouteOptions) -> Event:
    """Only the privacy level and route options; never the messages."""
    return Event(
        event_type="request.received",
        actor="core",
        privacy=request.privacy,
        allow_cloud=options.allow_cloud,
        include_sensitive=options.include_sensitive,
        override=options.override,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def route_selected(decision: RouteDecision, request: ModelRequest, options: RouteOptions) -> Event:
    return Event(
        event_type="model.route.selected",
        actor="router",
        privacy=request.privacy,
        provider=decision.provider,
        provider_kind=decision.kind,
        route_reason=decision.reason,
        allow_cloud=options.allow_cloud,
        include_sensitive=options.include_sensitive,
        override=options.override,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def call_completed(
    response: ModelResponse, request: ModelRequest, *, route_reason: str | None = None
) -> Event:
    """Provider, model, finish reason, latency and token counts; never the text.
    ``route_reason`` marks a call that is not the answer (the memory proposal call,
    ADR-033)."""
    usage = response.usage
    return Event(
        event_type="model.call.completed",
        actor="router",
        outcome="refusal" if response.finish_reason == "refusal" else "ok",
        privacy=request.privacy,
        provider=response.provider,
        provider_kind=response.kind,
        model=response.model,
        finish_reason=response.finish_reason,
        route_reason=route_reason,
        latency_ms=response.latency_ms,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def _event_attempt(attempt: Attempt) -> EventAttempt:
    return EventAttempt(
        provider=attempt.provider,
        error_type=attempt.error_type,
        message=attempt.message,
        retryable=attempt.retryable,
    )


def provider_unavailable(
    attempt: Attempt, request: ModelRequest, *, route_reason: str | None = None
) -> Event:
    """One failed call: type and scrubbed message only (``Attempt`` holds no exception).
    ``route_reason`` as in ``call_completed``."""
    return Event(
        event_type="provider.unavailable",
        actor="router",
        outcome="error",
        privacy=request.privacy,
        provider=attempt.provider,
        error_type=attempt.error_type,
        error_message=attempt.message,
        route_reason=route_reason,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


UNEXPECTED_ERROR_REASON = "unexpected error in Core (a bug); the type is in error_type"
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def request_crashed(exc: BaseException, request: ModelRequest, options: RouteOptions) -> Event:
    """``request.failed`` for an exception routing didn't expect (#112): the type name
    only, never the message, and a fixed reason. Same event type and columns as
    ``request_failed``, so the schema doesn't change."""
    name = type(exc).__name__
    return Event(
        event_type="request.failed",
        actor="core",
        outcome="error",
        privacy=request.privacy,
        route_reason=UNEXPECTED_ERROR_REASON,
        error_type=name if _TYPE_NAME.fullmatch(name) else "Exception",
        allow_cloud=options.allow_cloud,
        include_sensitive=options.include_sensitive,
        override=options.override,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def request_failed(
    exc: RouteFailed | NoRoute, request: ModelRequest, options: RouteOptions
) -> Event:
    attempts = exc.attempts if isinstance(exc, RouteFailed) else ()
    return Event(
        event_type="request.failed",
        actor="router",
        outcome="error",
        privacy=request.privacy,
        route_reason=exc.reason,
        error_type=type(exc).__name__,
        attempts=tuple(_event_attempt(a) for a in attempts) or None,
        allow_cloud=options.allow_cloud,
        include_sensitive=options.include_sensitive,
        override=options.override,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


@dataclass(frozen=True, slots=True)
class MemoryRecordMeta:
    """What a record event may say about a record: its ID, category, source and
    level. There is no field for content, a key or corrected text."""

    record_id: UUID
    category: Category
    source: Source
    level: NonSecretLevel


def memory_record_event(
    event_type: MemoryRecordEventType,
    record: MemoryRecordMeta,
    *,
    old_level: NonSecretLevel | None = None,
    actor: MemoryActor = "memory",
) -> Event:
    """One memory record changed (ADR-024 §5). ``old_level`` is for ``memory.updated``
    when the level changed."""
    return Event(
        event_type=event_type,
        actor=actor,
        memory_record_id=record.record_id,
        memory_category=record.category,
        memory_source=record.source,
        memory_level=record.level,
        memory_old_level=old_level,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def memory_exported(count: int, *, actor: MemoryActor = "memory") -> Event:
    """The owner exported memory (ADR-024 §4, §5): the number of records only, never
    their IDs, levels or content."""
    return Event(
        event_type="memory.exported",
        actor=actor,
        memory_export_count=count,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def memory_retrieved(use: MemoryUse) -> Event:
    """The memory selected for one call (ADR-024 §5): counts, characters, the
    effective level, record IDs and how many were left out. Never content."""
    memory = use.memory
    counts = memory.count_by_level()
    return Event(
        event_type="memory.context.retrieved",
        actor="memory",
        provider=use.provider,
        memory_count_public=counts["public"],
        memory_count_personal=counts["personal"],
        memory_count_sensitive=counts["sensitive"],
        memory_chars=memory.chars,
        memory_effective_level=use.effective_privacy,
        memory_record_ids=memory.record_ids,
        memory_left_out=memory.left_out,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


# --- writing ----------------------------------------------------------------------


async def write_memory_event(conn: AsyncConnection, event: Event) -> int:
    """Insert one memory event on ``conn``, inside the caller's transaction, so it
    commits or rolls back together with the change it records. Raises on any error.

    Memory events have no free-text field, so there is nothing to redact or
    echo-check; an event that has one, or isn't a memory event, is refused.
    """
    if event.event_type not in MEMORY_EVENT_TYPES:
        raise ValueError("not a memory event")
    if (event.route_reason, event.error_message, event.attempts) != (None, None, None):
        raise ValueError("memory events carry no free text")
    # Never set on a memory event: left out of the insert, so memory changes keep
    # working on a database not yet migrated to 0008.
    row = event.model_dump(mode="python", exclude={"include_sensitive"})
    result = await conn.execute(insert(events_table).values(**row).returning(events_table.c.id))
    return int(result.scalar_one())


def forbidden_texts(request: ModelRequest | None, also: Iterable[str] = ()) -> list[str]:
    """Every sent text the echo check looks for: the request's and ``also``."""
    texts = request_texts(request) if request is not None else []
    return texts + long_enough(also)


def _clean(text: str | None, redact: Redactor, forbidden: Sequence[str]) -> str | None:
    if text is None:
        return None
    cleaned = redact(text)
    if any(f in text or f in cleaned for f in forbidden):
        return REMOVED_ECHO
    return _cap(cleaned)


def to_row(event: Event, redact: Redactor, forbidden: Iterable[str] = ()) -> dict[str, Any]:
    """The exact values that will be stored. Free text is redacted and echo-checked."""
    forbid = list(forbidden)
    row = event.model_dump(mode="python")
    if row["include_sensitive"] is None:
        del row["include_sensitive"]  # migration 0008: only set where it means something
    row["route_reason"] = _clean(event.route_reason, redact, forbid)
    row["error_message"] = _clean(event.error_message, redact, forbid)
    if event.attempts is not None:
        row["attempts"] = [
            {**a.model_dump(mode="json"), "message": _clean(a.message, redact, forbid)}
            for a in event.attempts
        ]
    return row


class EventWriter:
    """Inserts events. Raises on database errors; the caller decides what to do."""

    def __init__(self, engine: AsyncEngine, redactor: Redactor) -> None:
        self._engine = engine
        self._redact = redactor

    async def write_with(
        self,
        conn: AsyncConnection,
        event: Event,
        *,
        request: ModelRequest | None = None,
        also_forbidden: Sequence[str] = (),
    ) -> int:
        """Insert on a connection the caller manages (and may roll back).
        ``also_forbidden``: more sent text for the echo check (memory text)."""
        forbidden = forbidden_texts(request, also_forbidden)
        row = to_row(event, self._redact, forbidden)
        result = await conn.execute(insert(events_table).values(**row).returning(events_table.c.id))
        return int(result.scalar_one())

    async def write_all(
        self,
        events: Sequence[Event],
        *,
        request: ModelRequest | None = None,
        also_forbidden: Sequence[str] = (),
    ) -> list[int]:
        """Insert several events in one transaction (all or none)."""
        async with self._engine.begin() as conn:
            return [
                await self.write_with(conn, e, request=request, also_forbidden=also_forbidden)
                for e in events
            ]


class EventRecorder:
    """Best-effort event recording for request handlers.

    ``record()`` never raises: if writing fails or takes longer than
    ``WRITE_TIMEOUT_S``, it logs one warning with the error type only (no
    message, no traceback; a driver error could echo connection details) and
    the request is unaffected. Without a writer (no DATABASE_URL) it does nothing.
    """

    def __init__(self, writer: EventWriter | None, timeout_s: float = WRITE_TIMEOUT_S) -> None:
        self._writer = writer
        self._timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    async def record(
        self,
        events: Sequence[Event],
        request: ModelRequest | None = None,
        also_forbidden: Sequence[str] = (),
    ) -> None:
        """``also_forbidden``: memory text sent in the request's calls (echo check);
        read when the write runs, so a list filled during the request works."""
        if self._writer is None or not events:
            return
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._writer.write_all(
                    events, request=request, also_forbidden=tuple(also_forbidden)
                )
        except Exception as exc:
            logger.warning(
                "Event write failed",
                extra={
                    "error_type": type(exc).__name__,
                    "event_types": [e.event_type for e in events],
                },
            )
