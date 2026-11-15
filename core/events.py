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
- On write, every free-text field (reason, error message, attempt messages)
  goes through the log ``Redactor`` (configured secrets, key and URL-credential
  patterns), and is blanked if it contains any of the request's own text
  (backstop against an adapter echoing the prompt).
"""

import asyncio
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.log import Redactor
from core.request_context import get_request_id, get_session_id
from router.contract import FinishReason, ModelRequest, ModelResponse, PrivacyLevel, ProviderKind
from router.routing import Attempt, NoRoute, RouteDecision, RouteFailed, RouteOptions

logger = logging.getLogger("aether.core.events")

SCHEMA_VERSION = 1
# One request's events are written in one transaction; a slow or unreachable
# database must not hold anything up for longer than this.
WRITE_TIMEOUT_S = 3.0
MAX_TEXT = 500
# Request texts shorter than this are not used for the echo backstop: blanking
# every field that contains "hi" would destroy ordinary reasons.
MIN_ECHO_LENGTH = 8
REMOVED_ECHO = "[REMOVED: contained request text]"

EventType = Literal[
    "request.received",
    "model.route.selected",
    "model.call.completed",
    "provider.unavailable",
    "request.failed",
]
Actor = Literal["core", "router"]
Outcome = Literal["ok", "refusal", "error"]

SafeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
ProviderName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,32}$")]
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:/-]{1,200}$")]
TypeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
SafeText = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
NonNegInt = Annotated[int, Field(ge=0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventAttempt(_Frozen):
    provider: ProviderName
    error_type: TypeName
    message: SafeText
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
    override: ProviderName | None = None
    provider: ProviderName | None = None
    provider_kind: ProviderKind | None = None
    model: ModelName | None = None
    route_reason: SafeText | None = None
    finish_reason: FinishReason | None = None
    latency_ms: Annotated[float, Field(ge=0.0)] | None = None
    input_tokens: NonNegInt | None = None
    output_tokens: NonNegInt | None = None
    error_type: TypeName | None = None
    error_message: SafeText | None = None
    attempts: Annotated[tuple[EventAttempt, ...], Field(max_length=2)] | None = None


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
        route_reason=_cap(decision.reason),
        allow_cloud=options.allow_cloud,
        override=options.override,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


def call_completed(response: ModelResponse, request: ModelRequest) -> Event:
    """Provider, model, finish reason, latency and token counts; never the text."""
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
        message=_cap(attempt.message),
        retryable=attempt.retryable,
    )


def provider_unavailable(attempt: Attempt, request: ModelRequest) -> Event:
    """One failed call: type and scrubbed message only (``Attempt`` holds no exception)."""
    return Event(
        event_type="provider.unavailable",
        actor="router",
        outcome="error",
        privacy=request.privacy,
        provider=attempt.provider,
        error_type=attempt.error_type,
        error_message=_cap(attempt.message),
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
        route_reason=_cap(exc.reason),
        error_type=type(exc).__name__,
        attempts=tuple(_event_attempt(a) for a in attempts) or None,
        allow_cloud=options.allow_cloud,
        override=options.override,
        request_id=get_request_id(),
        session_id=get_session_id(),
    )


# --- writing ----------------------------------------------------------------------


def request_texts(request: ModelRequest) -> list[str]:
    """The request's own text, for the echo backstop (never stored)."""
    texts = [m.content for m in request.messages]
    if request.system:
        texts.append(request.system)
    return [t for t in texts if len(t) >= MIN_ECHO_LENGTH]


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

    async def write(self, event: Event, *, request: ModelRequest | None = None) -> int:
        async with self._engine.begin() as conn:
            return await self.write_with(conn, event, request=request)

    async def write_with(
        self, conn: AsyncConnection, event: Event, *, request: ModelRequest | None = None
    ) -> int:
        """Insert on a connection the caller manages (and may roll back)."""
        forbidden = request_texts(request) if request is not None else []
        row = to_row(event, self._redact, forbidden)
        result = await conn.execute(insert(events_table).values(**row).returning(events_table.c.id))
        return int(result.scalar_one())

    async def write_all(
        self, events: Sequence[Event], *, request: ModelRequest | None = None
    ) -> list[int]:
        """Insert several events in one transaction (all or none)."""
        async with self._engine.begin() as conn:
            return [await self.write_with(conn, e, request=request) for e in events]


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

    async def record(self, events: Sequence[Event], request: ModelRequest | None = None) -> None:
        if self._writer is None or not events:
            return
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._writer.write_all(events, request=request)
        except Exception as exc:
            logger.warning(
                "Event write failed",
                extra={
                    "error_type": type(exc).__name__,
                    "event_types": [e.event_type for e in events],
                },
            )
