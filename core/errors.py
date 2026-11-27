"""App-wide error responses: one JSON shape for every error Core returns (#31).

Every error body is ``{"request_id": ..., "error": {"type", "message", "attempts"}}``.
422 validation errors also carry ``error.fields``: the location and message of each
problem, never pydantic's ``input`` or ``ctx`` (they can echo the request, docs/05 §5).

Used by the auth middleware (``core/auth.py``), the body-size guard and ``/chat``
(``core/chat.py``), and installed app-wide by ``create_app`` (``core/app.py``).
"""

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.request_context import get_request_id


class AttemptInfo(BaseModel):
    """One failed provider call: never the exception itself."""

    provider: str
    error_type: str
    message: str


class FieldError(BaseModel):
    """One validation problem: where it is and what is wrong, never the input."""

    loc: list[str]
    msg: str


class ErrorInfo(BaseModel):
    type: str
    message: str
    attempts: list[AttemptInfo] = []
    # Only on 422 validation errors; error_json() leaves it out of the JSON otherwise.
    fields: list[FieldError] | None = Field(
        default=None,
        description="Present only on 422 validation errors (location and message per "
        "problem, never the input); omitted from every other error.",
    )


class ErrorResponse(BaseModel):
    request_id: str | None
    error: ErrorInfo


def error_json(status: int, error_type: str, message: str, **extra: Any) -> JSONResponse:
    body = ErrorResponse(
        request_id=get_request_id(), error=ErrorInfo(type=error_type, message=message, **extra)
    )
    # ``fields`` is omitted, not null, when unset, so non-422 bodies are unchanged.
    exclude = {"error": {"fields"}} if body.error.fields is None else None
    return JSONResponse(status_code=status, content=body.model_dump(exclude=exclude))


# Stands in for the name of a field the request sent but the model doesn't have.
UNKNOWN_FIELD = "<unknown field>"


def api_field_names(app: Any) -> frozenset[str]:
    """Every property and parameter name in the app's own API schema: names AETHER
    defines, so echoing one tells the sender nothing they didn't send and nothing
    of theirs. Built once per app."""
    cached = getattr(app.state, "aether_api_field_names", None)
    if cached is None:
        schema = app.openapi()
        names: set[str] = set()
        for component in schema.get("components", {}).get("schemas", {}).values():
            names.update(component.get("properties", {}))
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                if isinstance(operation, dict):
                    names.update(p["name"] for p in operation.get("parameters", ()))
        cached = frozenset(names)
        app.state.aether_api_field_names = cached
    return cached


def _safe_loc(error: Any, known: frozenset[str]) -> list[str]:
    loc = [str(part) for part in error.get("loc", ())]
    # An unknown field's name is the sender's own text (a body key, a query name,
    # e.g. "diagnosis.migraine"), so it is echoed only if it is one of the API's
    # own names, such as "source" sent where it isn't allowed (#94, docs/05 §5).
    if error.get("type") == "extra_forbidden" and loc and loc[-1] not in known:
        loc[-1] = UNKNOWN_FIELD
    return loc


def _safe_validation_errors(errors: Any, known: frozenset[str]) -> list[FieldError]:
    # Location and message only: pydantic's "input"/"ctx" can echo the request.
    return [FieldError(loc=_safe_loc(e, known), msg=str(e.get("msg", ""))) for e in errors]


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    errors: Any = exc.errors() if isinstance(exc, RequestValidationError) else []
    fields = _safe_validation_errors(errors, api_field_names(request.app))
    return error_json(422, "ValidationError", "invalid request", fields=fields)
