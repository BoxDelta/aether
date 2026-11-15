"""AETHER_CLOUD_MAX_OUTPUT_TOKENS (#77): parsing, defaults and the startup check.

A bad value must stop Core at startup with an error that names the setting but
never shows the value from .env.
"""

from pathlib import Path

import pytest

from core.app import create_app
from core.config import (
    CLOUD_MAX_OUTPUT_TOKENS_ERROR,
    CLOUD_MAX_OUTPUT_TOKENS_LIMIT,
    DEFAULT_CLOUD_MAX_OUTPUT_TOKENS,
    ConfigError,
    Settings,
)


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_default_is_4096() -> None:
    assert DEFAULT_CLOUD_MAX_OUTPUT_TOKENS == 4096
    assert make().cloud_max_output_tokens == 4096


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_means_default(blank: str) -> None:
    # `AETHER_CLOUD_MAX_OUTPUT_TOKENS=` as in .env.example.
    assert make(aether_cloud_max_output_tokens=blank).cloud_max_output_tokens == 4096


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("2048", 2048), (" 8192 ", 8192)])
def test_valid_values(raw: str, expected: int) -> None:
    assert make(aether_cloud_max_output_tokens=raw).cloud_max_output_tokens == expected


def test_upper_limit_is_accepted() -> None:
    raw = str(CLOUD_MAX_OUTPUT_TOKENS_LIMIT)
    assert make(aether_cloud_max_output_tokens=raw).cloud_max_output_tokens == 32_000


def test_environment_variable_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_CLOUD_MAX_OUTPUT_TOKENS", "1024")
    assert make().cloud_max_output_tokens == 1024


def test_env_file_is_read(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("AETHER_CLOUD_MAX_OUTPUT_TOKENS=512\n")
    assert Settings(_env_file=env).cloud_max_output_tokens == 512


BAD_VALUES = [
    "0",
    "-5",
    "12.5",
    "4e3",
    "abc",
    "+7",
    "4_096",
    "32001",
    "99999999999999999999",
    "\uff11\uff12",  # full-width digits are not ASCII 0-9
]


@pytest.mark.parametrize("raw", BAD_VALUES)
def test_invalid_values_raise_config_error_without_the_value(raw: str) -> None:
    settings = make(aether_cloud_max_output_tokens=raw)  # loading itself never fails

    with pytest.raises(ConfigError) as excinfo:
        _ = settings.cloud_max_output_tokens

    # The message is fixed text: it names the setting and the allowed range only.
    assert str(excinfo.value) == CLOUD_MAX_OUTPUT_TOKENS_ERROR
    assert "AETHER_CLOUD_MAX_OUTPUT_TOKENS" in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "12.5"])
def test_core_fails_at_startup_on_invalid_value(raw: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        create_app(make(aether_cloud_max_output_tokens=raw))

    assert str(excinfo.value) == CLOUD_MAX_OUTPUT_TOKENS_ERROR


def test_startup_error_does_not_show_a_distinctive_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # A value that can't be a substring of the fixed message by accident.
    monkeypatch.setenv("AETHER_CLOUD_MAX_OUTPUT_TOKENS", "QXZ-not-a-number-QXZ")

    with pytest.raises(ConfigError) as excinfo:
        create_app(make())

    assert "QXZ" not in str(excinfo.value)
    assert "QXZ" not in repr(excinfo.value)


def test_core_starts_with_the_default() -> None:
    app = create_app(make())

    assert app.state.settings.cloud_max_output_tokens == 4096
