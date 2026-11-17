"""FastAPI application factory for AETHER Core."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from core import __version__
from core.auth import BearerAuth, configured_token
from core.chat import CHAT_PATH, MAX_BODY_BYTES, BodySizeLimit, chat_router
from core.config import Settings, get_settings
from core.db import (
    PRIVILEGED_MESSAGE,
    DatabaseConfigError,
    PrivilegedRoleError,
    check_database,
    create_engine,
)
from core.errors import validation_error_handler
from core.events import EventRecorder, EventWriter
from core.identity import Identity, load_identity
from core.log import Redactor, configure_logging, secrets_from
from core.ready import ready_router
from core.request_context import RequestContextMiddleware
from router.routing import Router

logger = logging.getLogger("aether.core.app")


class HealthResponse(BaseModel):
    """Liveness response. Reports only that the process is serving requests."""

    status: Literal["ok"]
    version: str


def _event_recorder(
    settings: Settings, redactor: Redactor
) -> tuple[EventRecorder, AsyncEngine | None]:
    """Events need DATABASE_URL; without it (or with a malformed one) they are
    skipped, with one warning here at startup."""
    if settings.database_url is None:
        logger.warning("Events disabled: DATABASE_URL is not set")
        return EventRecorder(None), None
    try:
        engine = create_engine(settings)  # does not connect yet
    except PrivilegedRoleError:
        raise  # Core refuses to start as the owner role (#1, #12)
    except DatabaseConfigError as exc:
        # The message is built from safe parts and never contains the URL.
        logger.warning("Events disabled: %s", exc)
        return EventRecorder(None), None
    return EventRecorder(EventWriter(engine, redactor)), engine


async def _refuse_privileged_role(settings: Settings, engine: AsyncEngine) -> None:
    """Startup check: raise ``PrivilegedRoleError`` if ``DATABASE_URL`` is the owner
    or another privileged role. Any other database problem (down, wrong password)
    doesn't stop Core: events are skipped and ``/ready`` reports it, as before."""
    status = await check_database(settings, engine)
    if status.message == PRIVILEGED_MESSAGE:
        raise PrivilegedRoleError(PRIVILEGED_MESSAGE)
    if not status.ok:
        logger.warning("Database check at startup: %s", status.problem)


def create_app(
    settings: Settings | None = None,
    identity: Identity | None = None,
    *,
    router: Router | None = None,
    events: EventRecorder | None = None,
) -> FastAPI:
    """Build the Core app. Called by uvicorn with ``--factory`` and by tests.

    Tests pass explicit ``settings`` so they never read the developer's ``.env``,
    and may pass a ``router`` (fake providers) and ``events`` recorder.
    Without ``identity``, the default identity file is loaded; an invalid file
    raises ``IdentityError`` so Core fails at startup rather than at the first call.
    An invalid ``AETHER_CLOUD_MAX_OUTPUT_TOKENS`` raises ``ConfigError`` the same way.
    A ``DATABASE_URL`` with the owner role raises ``PrivilegedRoleError``: here when
    it uses the same role as ``DATABASE_MIGRATION_URL``, at startup (lifespan) when
    the connected role turns out to be privileged.
    """
    settings = settings if settings is not None else get_settings()
    # A bad AETHER_CLOUD_MAX_OUTPUT_TOKENS raises ConfigError here, at startup.
    cloud_cap = settings.cloud_max_output_tokens
    configure_logging(settings)
    identity = identity if identity is not None else load_identity()
    logger.info(
        "Identity loaded",
        extra={"identity_name": identity.name, "identity_version": identity.version},
    )
    logger.info("Cloud output cap", extra={"cloud_max_output_tokens": cloud_cap})
    redactor = Redactor(secrets_from(settings))
    engine: AsyncEngine | None = None
    if events is None:
        events, engine = _event_recorder(settings, redactor)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if engine is not None:
                await _refuse_privileged_role(settings, engine)
            yield
        finally:
            # Shutdown (or a refused start): close provider HTTP clients, then the
            # DB pool. Each step is guarded so one failure doesn't skip the other.
            try:
                await app.state.router.aclose()
            finally:
                if engine is not None:
                    await engine.dispose()

    app = FastAPI(title="AETHER Core", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.identity = identity
    app.state.redactor = redactor
    app.state.router = router if router is not None else Router.from_settings(settings, identity)
    app.state.events = events
    # Shared DB pool (events and /ready); None without DATABASE_URL or with an
    # injected recorder (then /ready makes a short-lived engine).
    app.state.engine = engine

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Unhandled errors: RequestContextMiddleware (#7) logs them (redacted) and returns
    # a generic 500 with the request ID and no details.
    # Added last = outermost. Order per request: request IDs, then auth (before any
    # body is read), then the body-size check, then the route.
    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES, paths=frozenset({CHAT_PATH}))
    app.add_middleware(BearerAuth, token=configured_token(settings))
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    app.include_router(chat_router)
    app.include_router(ready_router)
    return app
