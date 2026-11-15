"""Per-request correlation IDs (request ID, session ID).

The middleware accepts a client-supplied ``X-Request-ID`` only if it is a short,
safe token; otherwise it generates one. The ID is echoed on the response and
stored in context variables so logging (#8) and events (#21) can read it without
passing it through every call.

Untrusted header values are validated before use so they cannot inject newlines
or control characters into logs or response headers (docs/05).
"""

import logging
import re
import uuid
from contextvars import ContextVar
from typing import TypeGuard

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
SESSION_ID_HEADER = "X-Session-ID"

# Letters, digits, '.', '_', '-'; 1-128 chars. Covers UUIDs, ULIDs and most tracer IDs.
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

logger = logging.getLogger("aether.core.request")

_ERROR_BODY = b'{"detail":"Internal Server Error"}'

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

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming_request_id = headers.get(REQUEST_ID_HEADER)
        incoming_session_id = headers.get(SESSION_ID_HEADER)

        request_id = incoming_request_id if is_safe_id(incoming_request_id) else new_request_id()
        session_id = incoming_session_id if is_safe_id(incoming_session_id) else None

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
        except Exception:
            # Log here, while the IDs are still in context; the server's own log of
            # this exception happens after they are reset.
            logger.exception("Unhandled exception", extra={"path": scope.get("path")})
            # Starlette builds its 500 response outside this middleware, so it would
            # lack the IDs. Send a generic 500 with the IDs (no error details), then
            # re-raise so the server still logs the exception.
            if not response_started:
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
            raise
        finally:
            _request_id.reset(request_token)
            _session_id.reset(session_token)
