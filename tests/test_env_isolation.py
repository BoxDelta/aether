"""Every Settings environment variable is cleared before each test (#38)."""

import os

import pytest

from core.config import Settings
from tests.conftest import ENV_VARS, OTHER_ENV_VARS, clear_env, settings_env_names

# Variables the old hand-written list cleared; the derived list must keep them all.
PREVIOUSLY_CLEARED = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "OLLAMA_HOST",
    "AETHER_OLLAMA_TRUST_HOST",
    "OLLAMA_MODEL",
    "LOG_LEVEL",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "AETHER_CLOUD_MAX_OUTPUT_TOKENS",
}


def test_every_settings_field_is_cleared() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    assert fields <= ENV_VARS, f"not cleared: {sorted(fields - ENV_VARS)}"
    assert {"AETHER_API_TOKEN", "AETHER_OLLAMA_TRUST_HOST"} <= ENV_VARS
    assert {"AETHER_CLOUD_MAX_OUTPUT_TOKENS"} <= settings_env_names()


def test_nothing_previously_cleared_was_dropped() -> None:
    assert PREVIOUSLY_CLEARED <= ENV_VARS
    assert OTHER_ENV_VARS <= ENV_VARS


@pytest.mark.parametrize("case", [str.upper, str.lower])
def test_clear_env_removes_each_settings_variable(
    monkeypatch: pytest.MonkeyPatch, case: object
) -> None:
    assert callable(case)
    names = sorted(settings_env_names())
    for name in names:
        monkeypatch.setenv(case(name), "from-the-shell")

    clear_env(monkeypatch)

    left = [k for k in os.environ if k.upper() in set(names)]
    assert left == [], f"still set after clear_env: {left}"
    settings = Settings(_env_file=None)
    for name, field in Settings.model_fields.items():
        assert getattr(settings, name) == field.default, name
