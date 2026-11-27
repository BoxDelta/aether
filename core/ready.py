"""Readiness: can Core actually answer? (#59)

``GET /ready`` reports one status word per component and an overall status.
Nothing else: no hosts, URLs, usernames, model lists, keys or exception text.
The response model only admits the fixed words below, so anything else fails
validation instead of reaching the client.

It sits behind the API token like every route except ``/health`` (ADR-021),
because it reveals what is configured.

Overall status:

- ``ready`` (200): Ollama answers with the default model pulled, a Claude key
  is configured, and Postgres is reachable and migrated to head.
- ``degraded`` (200): Ollama is fine, so default (local-only) requests work,
  but cloud isn't configured or the database (events) is down.
- ``not_ready`` (503): Ollama isn't usable for default requests. Default
  requests have ``allow_cloud=false``, so they would fail. That includes an
  Ollama host that is up but off-network (``ollama: "off_network"``, ADR-032):
  it answers only requests with ``allow_cloud``.

Every check runs concurrently under ``CHECK_TIMEOUT_S``, so ``/ready`` answers
within about that time even if something hangs.
"""

import asyncio
import logging
from collections.abc import Awaitable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db import Problem, check_database, engine_for, url_status
from router.contract import Provider
from router.routing import Router

logger = logging.getLogger("aether.core.ready")

READY_PATH = "/ready"
CHECK_TIMEOUT_S = 3.0
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

CheckFailure = Literal["timeout", "error"]
OllamaStatus = Literal["ok", "off_network", "unavailable", "not_configured"] | CheckFailure
# No network call: "configured" means a key is set, not that the API accepted it.
ClaudeStatus = Literal["configured", "not_configured"] | CheckFailure
DatabaseStatus = Problem | Literal["not_migrated"] | CheckFailure
Overall = Literal["ready", "degraded", "not_ready"]


class Components(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ollama: OllamaStatus
    claude: ClaudeStatus
    database: DatabaseStatus


class ReadyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Overall
    components: Components


@lru_cache(maxsize=1)
def migration_head() -> str | None:
    """The newest revision in ``migrations/`` (read from files, no database)."""
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()


async def _bounded[T](name: str, check: Awaitable[T], timeout_s: float) -> T | CheckFailure:
    try:
        async with asyncio.timeout(timeout_s):
            return await check
    except TimeoutError:
        return "timeout"
    except Exception as exc:
        # Type only: exception text can carry hosts or credentials.
        logger.warning(
            "Readiness check failed", extra={"component": name, "error_type": type(exc).__name__}
        )
        return "error"


async def _ollama(provider: Provider | None) -> OllamaStatus:
    if provider is None:
        return "not_configured"
    if not await provider.check_available():
        return "unavailable"
    # Up, but only for requests with allow_cloud (ADR-032): not usable by default.
    return "off_network" if provider.info.off_network else "ok"


async def _claude(provider: Provider | None) -> ClaudeStatus:
    if provider is None:
        return "not_configured"
    return "configured" if await provider.check_available() else "not_configured"


async def _database(settings: Settings, engine: AsyncEngine | None) -> DatabaseStatus:
    url = url_status(settings)
    if url is not None:
        return url.problem  # not_configured / misconfigured
    async with engine_for(settings, engine) as db:
        status = await check_database(settings, db)
        if not status.ok:
            return status.problem  # category only; status.message names the host
        async with db.connect() as conn:
            exists = await conn.scalar(text("SELECT to_regclass('public.alembic_version')"))
            current = (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                if exists
                else None
            )
    return "ok" if current is not None and current == migration_head() else "not_migrated"


def overall(components: Components) -> Overall:
    if components.ollama != "ok":
        return "not_ready"
    if components.claude == "configured" and components.database == "ok":
        return "ready"
    return "degraded"


async def check_ready(
    settings: Settings,
    router: Router,
    engine: AsyncEngine | None,
    timeout_s: float = CHECK_TIMEOUT_S,
) -> ReadyResponse:
    ollama, claude, database = await asyncio.gather(
        _bounded("ollama", _ollama(router.provider("ollama")), timeout_s),
        _bounded("claude", _claude(router.provider("claude")), timeout_s),
        _bounded("database", _database(settings, engine), timeout_s),
    )
    components = Components(ollama=ollama, claude=claude, database=database)
    return ReadyResponse(status=overall(components), components=components)


ready_router = APIRouter()


@ready_router.get(
    READY_PATH,
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "Ollama is not usable"}},
    tags=["system"],
)
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    result = await check_ready(state.settings, state.router, state.engine, CHECK_TIMEOUT_S)
    status_code = 503 if result.status == "not_ready" else 200
    return JSONResponse(status_code=status_code, content=result.model_dump())
