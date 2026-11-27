"""Per-request correlation IDs (request ID, session ID).

The middleware accepts a client-supplied ``X-Request-ID`` only if it is a short,
safe token; otherwise it generates one. The ID is echoed on the response and
stored in context variables so logging (#8) and events (#21) can read it without
passing it through every call.

Untrusted header values are validated before use so they cannot inject newlines
or control characters into logs or response headers (docs/05). With a
``looks_secret`` check (Core passes ``SecretCheck.from_settings``), an incoming ID
that contains a configured secret value is replaced too, so it is never echoed,
logged, put in an event or stored as a memory record's ``source_request_id`` (#75).

An unhandled exception is logged with its type and stack frames only, never its
message, which can hold request or response values (a ``ResponseValidationError``
quotes them). The server then sees a stand-in, ``UnhandledRequestError``, raised
``from None``, so its own log of the error carries no message either (#124).
"""

import contextlib
import logging
import re
import traceback
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, TypeGuard

from pydantic import StringConstraints
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
SESSION_ID_HEADER = "X-Session-ID"

# Letters, digits, '.', '_', '-'; 1-128 chars. Covers UUIDs, ULIDs and most tracer IDs.
# The one definition (#33): the middleware and the Event model both use it; the
# events table's CHECK constraint keeps a frozen copy.
SAFE_ID_PATTERN = r"[A-Za-z0-9._-]{1,128}"
_SAFE_ID = re.compile(SAFE_ID_PATTERN)
SafeId = Annotated[str, StringConstraints(pattern=rf"^{SAFE_ID_PATTERN}$")]

logger = logging.getLogger("aether.core.request")

_ERROR_BODY = b'{"detail":"Internal Server Error"}'


CHAIN_MAX_LINKS = 10  # chained causes and group members logged, at most
CHAIN_MAX_FRAMES = 30  # per link: the innermost frames, where it was raised


def exception_chain(exc: BaseException) -> str | None:
    """Type and frames of each exception linked to ``exc`` (#164), never a message:
    its explicit cause, its implicit context (unless suppressed with ``from None``)
    and the members of an exception group, depth first, at most ``CHAIN_MAX_LINKS``
    links of at most ``CHAIN_MAX_FRAMES`` frames each, and each
    exception once. ``None`` if there are none."""
    parts: list[str] = []
    seen = {id(exc)}
    pending: list[tuple[str, BaseException]] = list(reversed(_links(exc)))
    while pending and len(parts) < CHAIN_MAX_LINKS:
        how, linked = pending.pop()
        if id(linked) in seen:
            continue
        seen.add(id(linked))
        frames = "".join(traceback.format_tb(linked.__traceback__, limit=-CHAIN_MAX_FRAMES))
        parts.append(f"--- {how}: {type(linked).__name__} ---\n{frames}")
        pending.extend(reversed(_links(linked)))
    if any(id(linked) not in seen for _, linked in pending):
        parts.append("--- more links not logged ---\n")
    return "".join(parts) or None


def _links(exc: BaseException) -> list[tuple[str, BaseException]]:
    links: list[tuple[str, BaseException]] = []
    if isinstance(exc, BaseExceptionGroup):
        links.extend(("group member", member) for member in exc.exceptions)
    if exc.__cause__ is not None:
        links.append(("caused by", exc.__cause__))
    elif exc.__context__ is not None and not exc.__suppress_context__:
        links.append(("while handling", exc.__context__))
    return links


class UnhandledRequestError(Exception):
    """What the server sees instead of an unhandled exception: its type name only."""

    def __init__(self, error_type: str) -> None:
        super().__init__(f"unhandled {error_type} (details not logged, docs/05 §5)")
        self.error_type = error_type


_request_id: ContextVar[str | None] = ContextVar("aether_request_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("aether_session_id", default=None)


def get_request_id() -> str | None:
    """Request ID of the current request, or None outside a request."""
    return _request_id.get()


def get_session_id() -> str | None:
    """Session ID supplied by the client for the current request, if valid."""
    return _session_id.get()


def is_safe_id(value: str | None) -> TypeGuard[str]:
    return value is not None and _SAFE_ID.fullmatch(value) is not None


def new_request_id() -> str:
    return str(uuid.uuid4())


class RequestContextMiddleware:
    """Pure ASGI middleware, so the context variables are set in the same task as the endpoint."""

    def __init__(self, app: ASGIApp, looks_secret: Callable[[str], bool] | None = None) -> None:
        self.app = app
        self._looks_secret = looks_secret

    def _accept(self, value: str | None) -> str | None:
        if not is_safe_id(value):
            return None
        if self._looks_secret is not None and self._looks_secret(value):
            return None
        return value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming_request_id = headers.get(REQUEST_ID_HEADER)
        incoming_session_id = headers.get(SESSION_ID_HEADER)

        request_id = self._accept(incoming_request_id) or new_request_id()
        session_id = self._accept(incoming_session_id)

        response_started = False

        async def send_with_ids(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = MutableHeaders(scope=message)
                response_headers[REQUEST_ID_HEADER] = request_id
                if session_id is not None:
                    response_headers[SESSION_ID_HEADER] = session_id
            await send(message)

        request_token = _request_id.set(request_id)
        session_token = _session_id.set(session_id)
        try:
            await self.app(scope, receive, send_with_ids)
        except Exception as exc:
            # Log here, while the IDs are still in context; the server's own log of
            # this exception happens after they are reset. Type and frames only: the
            # message can hold values (#124). The frames' source lines are code.
            error_type = type(exc).__name__
            try:
                chain = exception_chain(exc)
            except Exception:
                chain = "<chain unavailable>"
            logger.error(
                "Unhandled exception",
                extra={
                    "path": scope.get("path"),
                    "error_type": error_type,
                    "frames": "".join(traceback.format_tb(exc.__traceback__)),
                    "chain": chain,
                },
            )
            # Starlette builds its 500 response outside this middleware, so it would
            # lack the IDs. Send a generic 500 with the IDs (no error details), then
            # re-raise so the server still logs the exception.
            # A failing send (the client went away) must not replace the stand-in:
            # its traceback would chain the original message back in (#124).
            if not response_started:
                with contextlib.suppress(Exception):
                    await send_with_ids(
                        {
                            "type": "http.response.start",
                            "status": 500,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(_ERROR_BODY)).encode()),
                            ],
                        }
                    )
                    await send_with_ids({"type": "http.response.body", "body": _ERROR_BODY})
            # ``from None`` hides the original from traceback printing; it is still on
            # ``__context__``, so an error reporter must honour __suppress_context__.
            raise UnhandledRequestError(error_type) from None
        finally:
            _request_id.reset(request_token)
            _session_id.reset(session_token)
