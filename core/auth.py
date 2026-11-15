"""Bearer-token authentication for every route except ``/health`` (ADR-021, #57).

- The token is ``AETHER_API_TOKEN`` from ``.env``. Clients send
  ``Authorization: Bearer <token>``.
- Fail closed: if the token is unset or too short, every protected route answers
  503 ``AuthNotConfigured`` and Core logs one warning at startup. Core never
  runs open.
- Comparison is constant-time (``hmac.compare_digest`` on bytes).
- The token never appears in a response body or a log line. Failures are logged
  with a reason category only (missing / malformed / invalid), never the
  header value. The token is also registered with the log redactor.
- Implemented as ASGI middleware rather than a route dependency, so it also
  covers ``/docs``, ``/openapi.json``, unknown paths, and any route added
  later. It runs before the body is read, so unauthenticated clients can't make
  Core buffer a request body.
"""

import hmac
import logging
from typing import Literal

from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import Settings
from core.errors import error_json

logger = logging.getLogger("aether.core.auth")

PUBLIC_PATHS = frozenset({"/health"})
# 32 characters: e.g. `openssl rand -hex 32` gives 64. Shorter tokens are refused
# (Core then serves no protected route) rather than accepted weakly.
MIN_TOKEN_LENGTH = 32
_SCHEME = b"bearer"

FailReason = Literal["missing", "malformed", "invalid"]


def configured_token(settings: Settings) -> bytes | None:
    """The token as bytes, or None (with one warning) if unset or too short.

    Checked here, not with a pydantic validator: validation errors keep the raw
    input, which would put the token into anything that logs the error.
    """
    if settings.aether_api_token is None:
        logger.warning("API disabled: AETHER_API_TOKEN is not set; only /health is served")
        return None
    token = settings.aether_api_token.get_secret_value()
    if len(token) < MIN_TOKEN_LENGTH or token != token.strip():
        logger.warning(
            "API disabled: AETHER_API_TOKEN must be at least %d characters with no "
            "surrounding spaces; only /health is served",
            MIN_TOKEN_LENGTH,
        )
        return None
    return token.encode("utf-8")


def check_authorization(header: bytes | None, token: bytes) -> FailReason | None:
    """None if ``header`` carries ``token``; otherwise why not."""
    if header is None:
        return "missing"
    scheme, _, credentials = header.strip().partition(b" ")
    credentials = credentials.strip()
    if scheme.lower() != _SCHEME or not credentials:
        return "malformed"
    if not hmac.compare_digest(credentials, token):
        return "invalid"
    return None


class BearerAuth:
    """ASGI middleware: 401 without the right token, 503 if none is configured."""

    def __init__(self, app: ASGIApp, token: bytes | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (
            scope["type"] == "http" and scope["path"] in PUBLIC_PATHS
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Fail closed: no WebSocket (or other) routes are authenticated yet.
            # Closing before accept makes the server answer 403.
            await send({"type": "websocket.close", "code": 1008})
            return

        if self.token is None:
            response = error_json(
                503, "AuthNotConfigured", "API token is not configured on the server"
            )
            await response(scope, receive, send)
            return

        header = next((v for k, v in scope["headers"] if k == b"authorization"), None)
        reason = check_authorization(header, self.token)
        if reason is None:
            await self.app(scope, receive, send)
            return

        logger.warning("Auth failed", extra={"reason": reason, "path": scope["path"]})
        response = error_json(401, "Unauthorized", "missing or invalid API token")
        response.headers["WWW-Authenticate"] = "Bearer"
        await response(scope, receive, send)
