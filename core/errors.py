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


def _safe_validation_errors(errors: Any) -> list[FieldError]:
    # Location and message only: pydantic's "input"/"ctx" can echo the request.
    return [
        FieldError(loc=[str(part) for part in e.get("loc", ())], msg=str(e.get("msg", "")))
        for e in errors
    ]


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    errors: Any = exc.errors() if isinstance(exc, RequestValidationError) else []
    return error_json(
        422, "ValidationError", "invalid request", fields=_safe_validation_errors(errors)
    )
