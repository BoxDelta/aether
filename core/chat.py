"""POST /chat: the first end-to-end slice (#22, ADR-005 thin orchestration).

request -> identity -> route -> provider -> response, with events recorded
after the response is sent (#21). The handler owns no business logic beyond
wiring: routing, identity and failover live in ``router/``.

Safety rules applied here:

- Defaults: ``privacy="personal"`` (docs/05 §7.1: unclassified is Personal),
  ``allow_cloud=false`` (cloud, and an off-network Ollama host, ADR-032, are
  opt-in per request).
- Size: the raw body is capped at ``MAX_BODY_BYTES`` before parsing, and the
  parsed request at ``MAX_MESSAGES`` / ``MAX_MESSAGE_CHARS`` / ``MAX_TOTAL_CHARS``.
- Output: ``max_tokens`` (optional) may lower the cloud output cap, never raise
  it (422). A cut-off answer is still a 200, with ``finish_reason="length"`` and
  its partial, possibly empty, text; the event records ``length`` too (#77).
- Errors: ``RouteFailed`` / ``NoRoute`` become a fixed JSON shape carrying only
  the error type, the routing reason and each attempt's provider, error type and
  scrubbed message; never ``__cause__``, exception text or a traceback.
  Validation errors never echo the submitted input.
- Tripwire (#8): a cloud send whose text looks like a secret is refused before
  anything is sent (``router/identity_call.py``). It is a 422
  ``SecretContentRefused`` with a fixed message, not a 502: the request, not a
  provider, is the problem. No failover; the text is never echoed.
- Memory (ADR-024 §3, step 12 of #58): each call gets its own selection inside the
  router (``include_sensitive`` defaults to false; Sensitive memory needs it), and
  one ``memory.context.retrieved`` event per call that could get memory. The
  events echo check covers the memory text sent as well as the request text.
- Memory proposals (ADR-033, step 15 of #58): only with ``propose_memory: true``,
  after an answer that finished normally, one more call to the same provider with
  the latest user message only and no memory (``memory/proposals.py``). Proposals
  are stored ``pending``; the reply carries only their count. A failure there
  never fails the answer.
- Access: bearer token, checked by core.auth before this module sees the request
  (ADR-021, #57).
- Events: metadata only (core/events.py). A failed event write never fails the
  request; it is logged as a warning with the error type only.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.errors import AttemptInfo, ErrorResponse, error_json
from core.events import (
    Event,
    EventRecorder,
    call_completed,
    memory_retrieved,
    provider_unavailable,
    request_failed,
    request_received,
    route_selected,
)
from core.log import Redactor
from core.request_context import get_request_id
from memory.proposals import parse, proposal_request, store_proposals
from memory.store import MemoryStore
from router.contract import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    ProviderError,
    ProviderKind,
    ProviderName,
    Role,
    SecretContentRefused,
)
from router.contract import Message as ModelMessage
from router.routing import (
    OFF_NETWORK_MARK,
    Attempt,
    MemoryUse,
    NoRoute,
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
    # Allows the cloud, and an Ollama host off this network (an untrusted
    # OLLAMA_HOST, ADR-032), which is treated like the cloud for privacy.
    allow_cloud: bool = False
    # Sensitive memory may be included (docs/07 §6). Never sent to the cloud or an
    # untrusted host whatever this says (ADR-024 D2, D4).
    include_sensitive: bool = False
    override: ProviderName | None = None
    # Ask the answering model for memory proposals (ADR-033). They are stored as
    # pending for the owner to approve; nothing is ever approved by a model.
    propose_memory: bool = False
    # Optional output token limit for this request (#77). It applies to whichever
    # provider answers; it may lower the cloud cap (AETHER_CLOUD_MAX_OUTPUT_TOKENS)
    # but never raise it: a larger value is rejected with 422 in the handler.
    max_tokens: Annotated[int, Field(ge=1, strict=True)] | None = None

    @model_validator(mode="after")
    def _total_size(self) -> "ChatRequest":
        if sum(len(m.content) for m in self.messages) > MAX_TOTAL_CHARS:
            raise ValueError(f"messages exceed {MAX_TOTAL_CHARS} characters in total")
        return self


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
    # Proposals stored as pending (ADR-033); null when propose_memory was not set.
    memory_proposals: int | None = None


# --- size guard ----------------------------------------------------------


class BodySizeLimit:
    """Refuse request bodies over ``max_bytes`` with 413, before parsing, on ``paths``
    and on every path that starts with one of ``prefixes``."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        paths: frozenset[str],
        prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = paths
        self.prefixes = prefixes

    def _covers(self, path: str) -> bool:
        return path in self.paths or path.startswith(self.prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._covers(scope["path"]):
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


# Marks the proposal call's own events, so they are never read as the answer's.
PROPOSAL_REASON = "memory proposal call (ADR-033), not the answer"


def _proposal_reason(router: Router, name: str) -> str:
    """The fixed proposal reason, marked like the answer's when the provider is an
    off-network Ollama host (#148)."""
    provider = router.provider(name)
    if provider is not None and provider.info.kind == "local" and provider.info.off_network:
        return PROPOSAL_REASON + OFF_NETWORK_MARK
    return PROPOSAL_REASON


async def _propose(  # noqa: PLR0913 - one value per input
    router: Router,
    store: MemoryStore | None,
    request: ModelRequest,
    options: RouteOptions,
    answer: ModelResponse,
    events: list[Event],
) -> int:
    """Ask the answering provider for memory proposals and store them as pending
    (ADR-033). Returns how many were stored. Never raises: proposals are optional.
    Logs one metadata-only line with the counts."""
    fields: dict[str, object] = {"provider": answer.provider}
    proposed = dropped = 0
    error_type: str | None = None
    prop = proposal_request(request)
    reason = _proposal_reason(router, answer.provider)
    try:
        if store is None:
            error_type = "MemoryNotConfigured"
        elif answer.finish_reason != "stop":
            error_type = f"answer_{answer.finish_reason}"
        elif prop is not None:
            try:
                reply = await router.call_once(answer.provider, prop, options)
            except ProviderError as exc:
                attempt = Attempt.from_error(exc)
                events.append(provider_unavailable(attempt, prop, route_reason=reason))
                raise
            events.append(call_completed(reply, prop, route_reason=reason))
            parsed = parse(reply.text) if reply.finish_reason == "stop" else None
            if parsed is None:
                dropped = 1
            else:
                stored = await store_proposals(
                    store,
                    parsed,
                    floor=request.privacy,
                    provider=answer.provider,
                    request_id=get_request_id(),
                )
                proposed, dropped = stored.proposed, stored.dropped
                error_type = stored.error_type
    except Exception as exc:  # proposals never fail the answer
        error_type = type(exc).__name__
    level = logger.warning if error_type and not error_type.startswith("answer_") else logger.info
    level(
        "Memory proposals",
        extra={**fields, "proposed": proposed, "dropped": dropped, "error_type": error_type},
    )
    return proposed


chat_router = APIRouter()


@chat_router.post(
    CHAT_PATH,
    response_model=ChatResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid API token"},
        413: {"model": ErrorResponse, "description": "Request body too large"},
        422: {
            "model": ErrorResponse,
            "description": "Invalid request (input not echoed), or SecretContentRefused: "
            "the text looks like a secret and was not sent to a provider off this network",
        },
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
    options = RouteOptions(
        allow_cloud=body.allow_cloud,
        override=body.override,
        include_sensitive=body.include_sensitive,
    )

    events: list[Event] = [request_received(request, options)]
    # Memory text sent in this request's calls, for the events echo check.
    memory_texts: list[str] = []
    # Runs after the response is sent; sees every event and text appended below.
    background.add_task(recorder.record, events, request, memory_texts)

    # Events are appended as things happen, so the list order and each occurred_at
    # match reality: received, selected, unavailable, selected (fallback), then
    # completed, or unavailable (fallback) and failed.
    def on_decision(decision: RouteDecision) -> None:
        events.append(route_selected(decision, request, options))

    def on_attempt_failed(attempt: Attempt) -> None:
        events.append(provider_unavailable(attempt, request))

    def on_memory(use: MemoryUse) -> None:
        memory_texts.extend(use.memory.texts)
        events.append(memory_retrieved(use))

    try:
        routed = await router.handle(
            request,
            options,
            on_decision=on_decision,
            on_attempt_failed=on_attempt_failed,
            on_memory=on_memory,
        )
    except RouteFailed as exc:
        events.append(request_failed(exc, request, options))
        refused = SecretContentRefused.__name__
        if any(a.error_type == refused for a in exc.attempts):
            return error_json(
                422,
                refused,
                SecretContentRefused.MESSAGE,
                attempts=[_attempt_info(a, redact) for a in exc.attempts],
            )
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
        failover_from = _attempt_info(routed.failed_first, redact)
    events.append(call_completed(response, request))

    proposals: int | None = None
    if body.propose_memory:
        proposals = await _propose(
            router, getattr(state, "memory", None), request, options, response, events
        )

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
        memory_proposals=proposals,
    )
