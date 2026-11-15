import logging
import os
from collections.abc import Iterator

import pytest

from core.config import Settings, get_settings
from core.identity import Identity, load_identity
from core.log import QUIET_LOGGERS


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
