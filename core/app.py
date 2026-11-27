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
from core.log import Redactor, SecretCheck, configure_logging, secrets_from
from core.memory_api import BODY_PATHS as MEMORY_BODY_PATHS
from core.memory_api import BODY_PREFIXES as MEMORY_BODY_PREFIXES
from core.memory_api import MAX_BODY_BYTES as MEMORY_MAX_BODY_BYTES
from core.memory_api import memory_router, memory_store
from core.ready import ready_router
from core.request_context import RequestContextMiddleware
from memory.retrieval import MemoryRetriever
from memory.store import MemoryStore
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
    memory: MemoryStore | None = None,
) -> FastAPI:
    """Build the Core app. Called by uvicorn with ``--factory`` and by tests.

    Tests pass explicit ``settings`` so they never read the developer's ``.env``,
    and may pass a ``router`` (fake providers), an ``events`` recorder and a
    ``memory`` store. Otherwise the memory store is built once, here, on the same
    engine as events, with ``SecretCheck.from_settings`` (#75); without a usable
    ``DATABASE_URL`` there is none and the memory routes answer 503 (#80). It is
    built only when Core builds its own events engine: with ``events`` injected and
    no ``memory``, the memory routes answer 503 even if ``DATABASE_URL`` is set.
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
    if memory is None and engine is not None:
        memory = memory_store(settings, engine)

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
    if router is None:
        # Memory for model calls (ADR-024 §3): only with a store. An injected router
        # brings its own MemorySource, or none.
        retriever = (
            MemoryRetriever(memory, identity.user_context_policy.include_by_default)
            if memory is not None
            else None
        )
        router = Router.from_settings(settings, identity, memory=retriever)
    app.state.router = router
    app.state.events = events
    app.state.memory = memory
    # Shared DB pool (events and /ready); None without DATABASE_URL or with an
    # injected recorder (then /ready makes a short-lived engine).
    app.state.engine = engine

    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Unhandled errors: RequestContextMiddleware (#7) logs them (redacted) and returns
    # a generic 500 with the request ID and no details.
    # Added last = outermost. Order per request: request IDs, then auth (before any
    # body is read), then the body-size check, then the route.
    app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES, paths=frozenset({CHAT_PATH}))
    app.add_middleware(
        BodySizeLimit,
        max_bytes=MEMORY_MAX_BODY_BYTES,
        paths=MEMORY_BODY_PATHS,
        prefixes=MEMORY_BODY_PREFIXES,
    )
    app.add_middleware(BearerAuth, token=configured_token(settings))
    # An incoming request ID holding a configured secret is replaced (#75).
    app.add_middleware(RequestContextMiddleware, looks_secret=SecretCheck.from_settings(settings))

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    app.include_router(chat_router)
    app.include_router(ready_router)
    app.include_router(memory_router)
    return app
