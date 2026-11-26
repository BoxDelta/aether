"""The memory API: list, get, search, owner create, approve, reject, correct, delete and
export (ADR-024 §2, §4; steps 6, 8, 9 and 10 of #58).

- ``GET /memory``: list, filtered only by ``status``, ``category`` and ``privacy``
  (fixed values, not content), paged.
- ``GET /memory/{id}``: one record.
- ``POST /memory/search``: by ``key`` and/or a text ``query``, in the body: the
  uvicorn access log records the query string, and a key or a phrase is content.
- ``POST /memory``: the owner creates an approved record. ``privacy`` is required
  (422 without it, never defaulted, Decision D1); ``secret`` and secret-shaped
  content are refused by the store (docs/05 §7.1).
- ``POST /memory/{id}/approve`` (step 8, #97): the owner approves a pending record
  with the final ``privacy`` (required); ``content``, ``category`` and ``key`` may be
  corrected in the same step (``key: null`` clears it). An approved record with the
  same key is replaced (deleted). 409 ``NotPending`` if it isn't pending.
- ``POST /memory/{id}/reject``: deletes a pending record (204). 409 ``NotPending``
  for an approved one (deleting those is ``DELETE``).
- ``PATCH /memory/{id}`` (step 9, #101): the owner corrects ``content``,
  ``category``, ``key``, ``privacy`` or ``expires_at``; the old text is overwritten
  (D6). ``key`` and ``expires_at`` may be ``null`` (cleared); nothing else may. A
  level set here is the owner's. 409 ``KeyConflict`` if another approved record
  has the key (only approve replaces). Expired records: 404.
- ``DELETE /memory/{id}``: hard delete of any record, expired or not (204).
- ``GET /memory/export`` (step 10, #122): one JSON document with every unexpired
  record, every column, all levels and pending ones (D10), plus one
  ``memory.exported`` event with the count. ``Cache-Control: no-store``. Expired
  records are left out; the export deletes nothing.

Rules applied here:

- **Who.** The bearer token means the owner in Phase 2 (ADR-024 §2, ADR-021), so
  ``source`` is always ``owner``. The body can't set ``source``, ``status`` or
  ``source_request_id`` (extra fields are refused); ``source_request_id`` is
  Core's request ID (``core.request_context``).
- **The store** is built once per app, from Settings, with
  ``SecretCheck.from_settings`` so the configured values are refused too (#75).
- **Database down** (#80; ADR-026 §5, proposed in #82): no database configured, or none reachable,
  is a 503 ``MemoryUnavailable`` with a fixed message: never an empty list, a 404
  or a write that did nothing.
- **Errors** carry the field and the rule, never the value: validation errors go
  through ``core/errors.py`` (no input echoed), store errors name a field and a
  rule only. Logs carry the operation and error type, never content or keys.
- **Size.** Bodies on ``/memory`` and every ``/memory/...`` path are capped at
  ``MAX_BODY_BYTES`` before parsing.

Not here yet: retrieval for model calls (steps 11-12), model proposals (step 15).
"""

import logging
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.errors import ErrorResponse, FieldError, error_json
from core.log import SecretCheck
from core.request_context import get_request_id
from memory.store import (
    LIST_LIMIT_DEFAULT,
    LIST_LIMIT_MAX,
    OWNER,
    QUERY_MAX,
    UNSET,
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
    PendingLimit,
    RecordChanges,
    RecordNotFound,
    StoreUnavailable,
)
from memory.types import Category, Status
from router.contract import PrivacyLevel

logger = logging.getLogger("aether.core.memory_api")

MEMORY_PATH = "/memory"
SEARCH_PATH = "/memory/search"
EXPORT_PATH = "/memory/export"
EXPORT_FORMAT: Literal["aether-memory"] = "aether-memory"
EXPORT_VERSION: Literal[1] = 1
BODY_PATHS = frozenset({MEMORY_PATH, SEARCH_PATH})
BODY_PREFIXES = (MEMORY_PATH + "/",)  # /memory/{id}/approve and the like
# Far more than any owner's memory; keeps OFFSET inside the database's bigint range,
# so a huge value is a 422 and never a 500 from the driver.
OFFSET_MAX = 1_000_000
# A record is at most 1000 characters (memory.store.CONTENT_MAX); 16 KiB is plenty
# even for 4-byte characters escaped in JSON.
MAX_BODY_BYTES = 16 * 1024
UNAVAILABLE_MESSAGE = "memory is unavailable: the database can't be reached"

Where = Literal["body", "query", "path"]


def memory_store(settings: Settings, engine: AsyncEngine) -> MemoryStore:
    """The one way Core builds its store: the secret check includes every
    configured secret value (#75)."""
    return MemoryStore(engine, SecretCheck.from_settings(settings))


# --- request / response shapes ------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportQuery(_Strict):
    """``GET /memory/export`` takes no filters: the export is always everything (D10).
    A query string is refused, so ``?privacy=public`` can't look like a filtered file,
    and a mistaken key or phrase in the URL (which the access log records) is loud."""


class CreateMemory(_Strict):
    """The owner's new record. Content and key limits are the store's own checks."""

    content: str
    category: Category
    # Required: no default, so a missing level is a 422 (Decision D1).
    privacy: PrivacyLevel
    key: str | None = None
    expires_at: AwareDatetime | None = None


class ListQuery(_Strict):
    """``GET /memory`` filters: fixed values only. Anything else in the query string
    (a ``key``, a ``query``) is refused, because the access log records it."""

    status: Status | None = None
    category: Category | None = None
    privacy: PrivacyLevel | None = None
    limit: Annotated[int, Field(ge=1, le=LIST_LIMIT_MAX)] = LIST_LIMIT_DEFAULT
    offset: Annotated[int, Field(ge=0, le=OFFSET_MAX)] = 0


class _Corrections(_Strict):
    """Fields left out don't change. Only those in ``NULLABLE`` may be sent as
    ``null`` (to clear them); ``null`` anywhere else is a 422, never "unchanged"."""

    NULLABLE: ClassVar[frozenset[str]] = frozenset({"key"})

    content: str | None = None
    category: Category | None = None
    key: str | None = None

    @model_validator(mode="after")
    def _no_null_except_clearable(self) -> Self:
        for name in self.model_fields_set - self.NULLABLE:
            if getattr(self, name) is None:
                raise ValueError(f"{name} can't be null")
        return self

    def given(self, name: str) -> Any:
        """The value sent for ``name``, or ``UNSET`` if it was left out."""
        return getattr(self, name) if name in self.model_fields_set else UNSET


class ApproveMemory(_Corrections):
    """The owner's approval: the final level, and optional corrections."""

    # Required: no record is approved with a level nobody chose (ADR-024 §1).
    privacy: PrivacyLevel


class PatchMemory(_Corrections):
    """The owner's correction (D6: overwrite). At least one field; the store says
    "nothing to change" otherwise."""

    NULLABLE: ClassVar[frozenset[str]] = frozenset({"key", "expires_at"})

    privacy: PrivacyLevel | None = None
    expires_at: AwareDatetime | None = None


class SearchMemory(_Strict):
    """Search by exact ``key`` and/or a case-insensitive substring ``query``."""

    key: str | None = None
    query: Annotated[str, Field(min_length=1, max_length=QUERY_MAX)] | None = None
    status: Status | None = None
    category: Category | None = None
    privacy: PrivacyLevel | None = None
    limit: Annotated[int, Field(ge=1, le=LIST_LIMIT_MAX)] = LIST_LIMIT_DEFAULT
    offset: Annotated[int, Field(ge=0, le=OFFSET_MAX)] = 0

    @model_validator(mode="after")
    def _something_to_find(self) -> "SearchMemory":
        if self.key is None and self.query is None:
            raise ValueError("give a key, a query or both")
        return self


class MemoryList(BaseModel):
    records: list[MemoryRecord]
    limit: int
    offset: int


class MemoryExportDocument(BaseModel):
    """The export format (ADR-024 §4). ``version`` changes only with the format."""

    format: Literal["aether-memory"] = EXPORT_FORMAT
    version: Literal[1] = EXPORT_VERSION
    exported_at: datetime
    records: list[MemoryRecord]


# --- errors ---------------------------------------------------------------------------


# Store errors with a fixed answer: (status, error type, message). Messages are fixed
# text or the store's own, which never holds content or keys.
_FIXED: tuple[tuple[type[MemoryStoreError], int, str, str | None], ...] = (
    (RecordNotFound, 404, "NotFound", "memory record not found"),
    (KeyConflict, 409, "KeyConflict", None),
    (NotPending, 409, "NotPending", "memory record is not pending"),
    # Not reachable yet: the API creates owner records only (proposals are step 15).
    (PendingLimit, 409, "PendingLimit", None),
    # Not reachable: the API always acts as the owner.
    (NotAllowed, 403, "NotAllowed", "not allowed"),
)


def _store_error(exc: MemoryStoreError, where: Where, operation: str) -> JSONResponse:
    """A fixed response for a store error. Store messages name a field and a rule
    (or an error type), never content or keys."""
    if isinstance(exc, InvalidRecord):
        loc = ["header", "X-Request-ID"] if exc.field == "source_request_id" else [where, exc.field]
        return error_json(
            422, "ValidationError", "invalid request", fields=[FieldError(loc=loc, msg=exc.rule)]
        )
    for kind, status, error_type, message in _FIXED:
        if isinstance(exc, kind):
            return error_json(status, error_type, message if message is not None else str(exc))
    unavailable = isinstance(exc, StoreUnavailable)
    level = logging.WARNING if unavailable else logging.ERROR
    reason = ": database unavailable" if unavailable else ""
    logger.log(
        level,
        "Memory %s failed%s",
        operation,
        reason,
        extra={"operation": operation, "error": str(exc)},
    )
    if unavailable:
        return _unavailable()
    return error_json(500, "MemoryStoreFailed", "the memory store failed")


def _unavailable() -> JSONResponse:
    return error_json(503, "MemoryUnavailable", UNAVAILABLE_MESSAGE)


def _store(request: Request) -> MemoryStore | None:
    store: MemoryStore | None = request.app.state.memory
    if store is None:
        logger.warning("Memory request refused: no database configured")
    return store


# --- routes ---------------------------------------------------------------------------

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid API token"},
    413: {"model": ErrorResponse, "description": "Request body too large"},
    422: {"model": ErrorResponse, "description": "Invalid request (input not echoed)"},
    503: {"model": ErrorResponse, "description": "Database unavailable (MemoryUnavailable)"},
}

memory_router = APIRouter(tags=["memory"])


@memory_router.get(MEMORY_PATH, response_model=MemoryList, responses=_ERRORS)
async def list_memory(
    params: Annotated[ListQuery, Query()], request: Request
) -> MemoryList | JSONResponse:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        filters = ListFilter(
            status=params.status,
            category=params.category,
            privacy=params.privacy,
            limit=params.limit,
            offset=params.offset,
        )
        records = await store.list(filters)
    except MemoryStoreError as exc:
        return _store_error(exc, "query", "list")
    return MemoryList(records=records, limit=params.limit, offset=params.offset)


@memory_router.post(SEARCH_PATH, response_model=MemoryList, responses=_ERRORS)
async def search_memory(body: SearchMemory, request: Request) -> MemoryList | JSONResponse:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        filters = ListFilter(
            key=body.key,
            query=body.query,
            status=body.status,
            category=body.category,
            privacy=body.privacy,
            limit=body.limit,
            offset=body.offset,
        )
        records = await store.list(filters)
    except MemoryStoreError as exc:
        return _store_error(exc, "body", "search")
    return MemoryList(records=records, limit=body.limit, offset=body.offset)


# Registered before GET /memory/{record_id}, which would otherwise take "export" as
# an ID (a 422).
@memory_router.get(EXPORT_PATH, response_model=MemoryExportDocument, responses=_ERRORS)
async def export_memory(
    _params: Annotated[ExportQuery, Query()], request: Request, response: Response
) -> MemoryExportDocument | Response:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        export = await store.export(actor=OWNER)
    except MemoryStoreError as exc:
        return _store_error(exc, "query", "export")
    # Every level, Sensitive included (D10): never kept by a cache on the way.
    response.headers["Cache-Control"] = "no-store"
    return MemoryExportDocument(exported_at=export.exported_at, records=list(export.records))


@memory_router.get(
    MEMORY_PATH + "/{record_id}",
    response_model=MemoryRecord,
    responses={**_ERRORS, 404: {"model": ErrorResponse, "description": "No such record"}},
)
async def get_memory(record_id: UUID, request: Request) -> MemoryRecord | JSONResponse:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        return await store.get(record_id)
    except MemoryStoreError as exc:
        return _store_error(exc, "path", "get")


@memory_router.post(
    MEMORY_PATH,
    response_model=MemoryRecord,
    status_code=201,
    responses={
        **_ERRORS,
        409: {"model": ErrorResponse, "description": "Another approved record has this key"},
    },
)
async def create_memory(body: CreateMemory, request: Request) -> MemoryRecord | JSONResponse:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        new = NewRecord(
            content=body.content,
            category=body.category,
            privacy=body.privacy,
            source=OWNER,  # the token means the owner (ADR-024 §2), never the body
            key=body.key,
            source_request_id=get_request_id(),
            expires_at=body.expires_at,
        )
        return await store.create(new)
    except MemoryStoreError as exc:
        return _store_error(exc, "body", "create")


_RECORD_ERRORS: dict[int | str, dict[str, object]] = {
    **_ERRORS,
    404: {"model": ErrorResponse, "description": "No such record (or expired)"},
}


def _conflict(description: str) -> dict[int | str, dict[str, object]]:
    """The 409 a route can really give, so the OpenAPI schema doesn't promise more."""
    return {409: {"model": ErrorResponse, "description": description}}


_APPROVE_ERRORS = {
    **_RECORD_ERRORS,
    **_conflict("NotPending, or KeyConflict (two approvals with one key at the same moment)"),
}
_REJECT_ERRORS = {**_RECORD_ERRORS, **_conflict("NotPending: only a pending record is rejected")}
_PATCH_ERRORS = {**_RECORD_ERRORS, **_conflict("KeyConflict: another approved record has the key")}
# DELETE has no 409: any record, expired or not, can be deleted.


@memory_router.post(
    MEMORY_PATH + "/{record_id}/approve", response_model=MemoryRecord, responses=_APPROVE_ERRORS
)
async def approve_memory(
    record_id: UUID, body: ApproveMemory, request: Request
) -> MemoryRecord | JSONResponse:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        approval = Approval(
            privacy=body.privacy,
            content=body.given("content"),
            category=body.given("category"),
            key=body.given("key"),
        )
        return await store.approve(record_id, approval, actor=OWNER)
    except MemoryStoreError as exc:
        return _store_error(exc, "body", "approve")


@memory_router.post(MEMORY_PATH + "/{record_id}/reject", status_code=204, responses=_REJECT_ERRORS)
async def reject_memory(record_id: UUID, request: Request) -> Response:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        await store.reject(record_id, actor=OWNER)
    except MemoryStoreError as exc:
        return _store_error(exc, "path", "reject")
    return Response(status_code=204)


@memory_router.patch(
    MEMORY_PATH + "/{record_id}", response_model=MemoryRecord, responses=_PATCH_ERRORS
)
async def patch_memory(
    record_id: UUID, body: PatchMemory, request: Request
) -> MemoryRecord | JSONResponse:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        changes = RecordChanges(
            content=body.given("content"),
            category=body.given("category"),
            key=body.given("key"),
            privacy=body.given("privacy"),
            expires_at=body.given("expires_at"),
        )
        return await store.update(record_id, changes, actor=OWNER)
    except MemoryStoreError as exc:
        return _store_error(exc, "body", "update")


@memory_router.delete(MEMORY_PATH + "/{record_id}", status_code=204, responses=_RECORD_ERRORS)
async def delete_memory(record_id: UUID, request: Request) -> Response:
    store = _store(request)
    if store is None:
        return _unavailable()
    try:
        await store.delete(record_id, actor=OWNER)
    except MemoryStoreError as exc:
        return _store_error(exc, "path", "delete")
    return Response(status_code=204)
