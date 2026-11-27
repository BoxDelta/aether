import asyncio
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from core.config import Settings, get_settings
from core.db import create_migration_engine, migration_url
from core.db_admin import scram_verifier
from core.identity import Identity, load_identity
from core.log import QUIET_LOGGERS

APP_ROLE = "aether_app"  # migrations/versions/0003_app_role.py


def settings_env_names() -> frozenset[str]:
    """The environment variable of every ``Settings`` field, upper-cased (#38).

    Derived from ``Settings.model_fields`` so a new setting is cleared without
    editing this file. Settings matches names case-insensitively, so clearing does too.
    """
    prefix = Settings.model_config.get("env_prefix") or ""
    names = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias if isinstance(field.validation_alias, str) else None
        names.add((alias or f"{prefix}{name}").upper())
    return frozenset(names)


# Read by SDKs or docker compose rather than Settings, but they still change behaviour.
OTHER_ENV_VARS = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "POSTGRES_PASSWORD",
    }
)

ENV_VARS = settings_env_names() | OTHER_ENV_VARS


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable in ``ENV_VARS``, in any letter case."""
    for key in list(os.environ):
        if key.upper() in ENV_VARS:
            monkeypatch.delenv(key)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of the developer's shell environment."""
    clear_env(monkeypatch)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """create_app() configures logging globally; undo it after each test."""
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access", *QUIET_LOGGERS)
    saved = {
        n: (
            list(logging.getLogger(n).handlers),
            logging.getLogger(n).level,
            logging.getLogger(n).propagate,
        )
        for n in names
    }
    yield
    for n, (handlers, level, propagate) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


# --- shared fixtures (#37) ------------------------------------------------------------


@pytest.fixture
def identity() -> Identity:
    return load_identity()


# Live Postgres tests (test_db_live.py, test_events_live.py): ``pytestmark = LIVE_DB``.
LIVE_DB = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get("AETHER_DB_TESTS") != "1",
        reason="live Postgres tests: set AETHER_DB_TESTS=1 (needs docker compose up -d)",
    ),
]


@pytest.fixture
def live() -> Settings:
    """The owner's real database settings, for the opted-in live tests only."""
    s = Settings()  # reads .env on purpose: the owner's local database
    if s.database_url is None:
        # Opted in with AETHER_DB_TESTS=1: a missing database is a failure, not a
        # skip, so CI can't pass without running these (#52).
        pytest.fail("AETHER_DB_TESTS=1 but DATABASE_URL is not set in .env")
    return s


@pytest.fixture
def live_owner(live: Settings) -> Settings:
    """The owner role (``DATABASE_MIGRATION_URL``, else ``DATABASE_URL`` as in CI) as
    the only URL. Use it with ``create_migration_engine``; Core's engine refuses it."""
    _, url = migration_url(live)
    return Settings(_env_file=None, database_url=url)


def _with_credentials(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    netloc = f"{user}:{password}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit(parts._replace(netloc=netloc))


async def _as_owner(owner: Settings, statement: str) -> None:
    engine = create_migration_engine(owner)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(statement)
    finally:
        await engine.dispose()


TEST_ROLE_PREFIX = "aether_app_test_"
SWEEP_MIN_AGE_S = 3600  # a younger test role may belong to a run going on now
_swept: list[bool] = []  # once per test session


def role_comment(at: float | None = None) -> str:
    """The note on each test role: when it was made (epoch seconds)."""
    return f"aether live test {int(time.time() if at is None else at)}"


async def _sweep_leftover_test_roles(owner: Settings) -> list[str]:
    """Drop ``aether_app_test_*`` roles a killed run left behind (#61): only those
    in the fixture's exact name shape, with no open session and made more than
    ``SWEEP_MIN_AGE_S`` ago; one that still owns or holds anything stays (the drop
    fails and is skipped). Returns the dropped names."""
    engine = create_migration_engine(owner)
    dropped: list[str] = []
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT rolname FROM pg_roles r WHERE starts_with(rolname, :prefix)"
                    " AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a"
                    " WHERE a.usename = r.rolname)"
                    # Made by this fixture over an hour ago (or before it noted the
                    # time): a run going on at the same time keeps its role.
                    " AND COALESCE(substring(shobj_description(r.oid, 'pg_authid')"
                    " FROM '^aether live test ([0-9]+)$')::bigint, 0)"
                    " < extract(epoch FROM now()) - :min_age"
                ),
                {"prefix": TEST_ROLE_PREFIX, "min_age": SWEEP_MIN_AGE_S},
            )
            names = [name for (name,) in rows]
        for name in names:
            if not re.fullmatch(rf"{TEST_ROLE_PREFIX}[0-9a-f]{{12}}", name):
                continue  # not one this fixture made
            try:
                await _as_owner_on(engine, f"DROP ROLE {name}")
            except Exception:  # noqa: S112 - it owns or holds something: leave it
                continue
            dropped.append(name)
    finally:
        await engine.dispose()
    return dropped


async def _as_owner_on(engine: Any, statement: str) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(statement)


@pytest.fixture
def live_app(live_owner: Settings) -> Iterator[Settings]:
    """Core's settings with a throwaway login role that is a member of the app role
    (``aether_app``), so it has exactly the app role's rights.

    Works the same in CI (where ``.env`` has only the owner URL) and on the owner's
    PC. The role gets a random password (also a canary: tests check it never shows
    up in output) and is dropped afterwards; it never owns anything.
    """
    assert live_owner.database_url is not None
    owner_url = live_owner.database_url.get_secret_value()
    if not _swept:
        _swept.append(True)
        asyncio.run(_sweep_leftover_test_roles(live_owner))
    role = f"{TEST_ROLE_PREFIX}{secrets.token_hex(6)}"
    password = secrets.token_hex(24)
    try:
        asyncio.run(
            _as_owner(
                live_owner,
                f"CREATE ROLE {role} LOGIN IN ROLE {APP_ROLE} "
                f"PASSWORD '{scram_verifier(password)}'",
            )
        )
        asyncio.run(_as_owner(live_owner, f"COMMENT ON ROLE {role} IS '{role_comment()}'"))
    except Exception as exc:
        pytest.fail(
            f"cannot create a test login role in {APP_ROLE} ({type(exc).__name__}); "
            "run `uv run alembic upgrade head` and set DATABASE_MIGRATION_URL"
        )
    try:
        yield Settings(
            _env_file=None,
            database_url=SecretStr(_with_credentials(owner_url, role, password)),
            database_migration_url=SecretStr(owner_url),
        )
    finally:
        asyncio.run(_as_owner(live_owner, f"DROP ROLE IF EXISTS {role}"))
