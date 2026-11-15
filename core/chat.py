"""POST /chat: the first end-to-end slice (#22, ADR-005 thin orchestration).

request -> identity -> route -> provider -> response, with events recorded
after the response is sent (#21). The handler owns no business logic beyond
wiring: routing, identity and failover live in ``router/``.

Safety rules applied here:

- Defaults: ``privacy="personal"`` (docs/05 §7.1: unclassified is Personal),
  ``allow_cloud=false`` (cloud is opt-in per request).
- Size: the raw body is capped at ``MAX_BODY_BYTES`` before parsing, and the
  parsed request at ``MAX_MESSAGES`` / ``MAX_MESSAGE_CHARS`` / ``MAX_TOTAL_CHARS``.
- Output: ``max_tokens`` (optional) may lower the cloud output cap, never raise
  it (422). A cut-off answer is still a 200, with ``finish_reason="length"`` and
  its partial, possibly empty, text; the event records ``length`` too (#77).
- Errors: ``RouteFailed`` / ``NoRoute`` become a fixed JSON shape carrying only
  the error type, the routing reason and each attempt's provider, error type and
  scrubbed message; never ``__cause__``, exception text or a traceback.
  Validation errors never echo the submitted input.
- Access: bearer token, checked by core.auth before this module sees the request
  (ADR-021, #57).
- Events: metadata only (core/events.py). A failed event write never fails the
  request; it is logged as a warning with the error type only.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.events import (
    Event,
    EventRecorder,
    call_completed,
    provider_unavailable,
    request_failed,
    request_received,
    route_selected,
)
from core.log import Redactor
from core.request_context import get_request_id
from router.contract import (
    FinishReason,
    ModelRequest,
    PrivacyLevel,
    ProviderKind,
    Role,
)
from router.contract import Message as ModelMessage
from router.routing import (
    Attempt,
    NoRoute,
    ProviderName,
    RouteDecision,
    RouteFailed,
    RouteOptions,
    Router,
)

logger = logging.getLogger("aether.core.chat")

CHAT_PATH = "/chat"
MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 20_000
MAX_TOTAL_CHARS = 60_000


# --- request / response shapes ------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(_Strict):
    role: Role
    content: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]


class ChatRequest(_Strict):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=MAX_MESSAGES)]
    privacy: PrivacyLevel = "personal"
    allow_cloud: bool = False
    override: ProviderName | None = None
    # Optional output token limit for this request (#77). It applies to whichever
    # provider answers; it may lower the cloud cap (AETHER_CLOUD_MAX_OUTPUT_TOKENS)
    # but never raise it: a larger value is rejected with 422 in the handler.
    max_tokens: Annotated[int, Field(ge=1, strict=True)] | None = None

    @model_validator(mode="after")
    def _total_size(self) -> "ChatRequest":
        if sum(len(m.content) for m in self.messages) > MAX_TOTAL_CHARS:
            raise ValueError(f"messages exceed {MAX_TOTAL_CHARS} characters in total")
        return self


class AttemptInfo(BaseModel):
    """One failed provider call: never the exception itself."""

    provider: str
    error_type: str
    message: str


class ChatResponse(BaseModel):
    request_id: str | None
    text: str
    provider: str
    provider_kind: ProviderKind
    model: str
    finish_reason: FinishReason
    route_reason: str
    # Set when the first provider failed and this answer came from the fallback.
    failover_from: AttemptInfo | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float


class ErrorInfo(BaseModel):
    type: str
    message: str
    attempts: list[AttemptInfo] = []


class ErrorResponse(BaseModel):
    request_id: str | None
    error: ErrorInfo


def error_json(status: int, error_type: str, message: str, **extra: Any) -> JSONResponse:
    body = ErrorResponse(
        request_id=get_request_id(), error=ErrorInfo(type=error_type, message=message, **extra)
    )
    return JSONResponse(status_code=status, content=body.model_dump())


# --- size guard ----------------------------------------------------------


class BodySizeLimit:
    """Refuse request bodies over ``max_bytes`` on ``paths`` with 413, before parsing."""

    def __init__(self, app: ASGIApp, max_bytes: int, paths: frozenset[str]) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in self.paths:
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await self._too_large(scope, receive, send)
                return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_bytes:
                await self._too_large(scope, receive, send)
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_json(
            413, "RequestTooLarge", f"request body is over {self.max_bytes} bytes"
        )
        await response(scope, receive, send)


# --- the endpoint --------------------------------------------------------------------


def _attempt_info(attempt: Attempt, redact: Redactor) -> AttemptInfo:
    # Attempt holds only ProviderError.message (adapters build it from safe parts);
    # redact again in case a configured secret slipped in.
    return AttemptInfo(
        provider=attempt.provider,
        error_type=attempt.error_type,
        message=redact(attempt.message),
    )


chat_router = APIRouter()


@chat_router.post(
    CHAT_PATH,
    response_model=ChatResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API token"},
        413: {"model": ErrorResponse, "description": "Request body too large"},
        422: {"model": ErrorResponse, "description": "Invalid request (input not echoed)"},
        502: {"model": ErrorResponse, "description": "Provider(s) failed (RouteFailed)"},
        503: {"model": ErrorResponse, "description": "No provider may take it (NoRoute)"},
    },
    tags=["chat"],
)
async def chat(
    body: ChatRequest, http_request: Request, background: BackgroundTasks
) -> ChatResponse | JSONResponse:
    state = http_request.app.state
    router: Router = state.router
    recorder: EventRecorder = state.events
    redact: Redactor = state.redactor

    cap: int = state.settings.cloud_max_output_tokens
    if body.max_tokens is not None and body.max_tokens > cap:
        # Same 422 shape as any validation error: location and message, no input.
        raise RequestValidationError(
            [
                {
                    "loc": ("body", "max_tokens"),
                    "msg": f"Input should be less than or equal to {cap}",
                    "type": "less_than_equal",
                }
            ]
        )

    request = ModelRequest(
        messages=tuple(ModelMessage(role=m.role, content=m.content) for m in body.messages),
        privacy=body.privacy,
        max_tokens=body.max_tokens,
    )
    options = RouteOptions(allow_cloud=body.allow_cloud, override=body.override)

    events: list[Event] = [request_received(request, options)]
    # Runs after the response is sent; sees every event appended below.
    background.add_task(recorder.record, events, request)

    def on_decision(decision: RouteDecision) -> None:
        events.append(route_selected(decision, request, options))

    try:
        routed = await router.handle(request, options, on_decision=on_decision)
    except RouteFailed as exc:
        events.extend(provider_unavailable(a, request) for a in exc.attempts)
        events.append(request_failed(exc, request, options))
        return error_json(
            502,
            "RouteFailed",
            redact(exc.reason),
            attempts=[_attempt_info(a, redact) for a in exc.attempts],
        )
    except NoRoute as exc:
        events.append(request_failed(exc, request, options))
        return error_json(503, "NoRoute", redact(exc.reason))

    response = routed.response
    failover_from = None
    if routed.failed_first is not None:
        # Event order: received, selected (first), unavailable (first),
        # selected (fallback), completed.
        events.insert(2, provider_unavailable(routed.failed_first, request))
        failover_from = _attempt_info(routed.failed_first, redact)
    events.append(call_completed(response, request))

    usage = response.usage
    return ChatResponse(
        request_id=get_request_id(),
        text=response.text,
        provider=response.provider,
        provider_kind=response.kind,
        model=response.model,
        finish_reason=response.finish_reason,
        route_reason=redact(routed.decision.reason),
        failover_from=failover_from,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        latency_ms=round(response.latency_ms, 1),
    )


# --- app-wide error handlers (installed by create_app) -------------------------------


def _safe_validation_errors(errors: Any) -> list[dict[str, Any]]:
    # Location and message only: pydantic's "input"/"ctx" can echo the request.
    return [
        {"loc": [str(part) for part in e.get("loc", ())], "msg": str(e.get("msg", ""))}
        for e in errors
    ]


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    errors: Any = exc.errors() if isinstance(exc, RequestValidationError) else []
    body = ErrorResponse(
        request_id=get_request_id(),
        error=ErrorInfo(type="ValidationError", message="invalid request"),
    ).model_dump()
    body["error"]["fields"] = _safe_validation_errors(errors)
    return JSONResponse(status_code=422, content=body)
