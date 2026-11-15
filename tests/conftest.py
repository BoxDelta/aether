import logging
from collections.abc import Iterator

import pytest

from core.config import get_settings
from core.log import QUIET_LOGGERS

ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "LOG_LEVEL",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "AETHER_CLOUD_MAX_OUTPUT_TOKENS",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of the developer's shell environment."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
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
