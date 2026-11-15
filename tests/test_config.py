import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.app import create_app
from core.config import Settings, get_settings

FAKE_KEY = "sk-ant-test-0123456789abcdef"  # not a real key  # gitleaks:allow


def make(**kwargs: object) -> Settings:
    """Settings without reading any .env file."""
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_defaults() -> None:
    settings = make()

    assert settings.anthropic_api_key is None
    assert str(settings.ollama_host) == "http://localhost:11434/"
    assert settings.ollama_model == "llama3.1:8b"
    assert settings.anthropic_model == "claude-opus-5-5"
    assert settings.log_level == "INFO"


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")

    settings = make()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == FAKE_KEY
    assert settings.ollama_host.host == "gpu-box"
    assert settings.log_level == "DEBUG"
    assert settings.ollama_model == "qwen2.5:7b"


def test_reads_dotenv_file_and_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OLLAMA_HOST=http://from-file:11434\nLOG_LEVEL=WARNING\n")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    settings = Settings(_env_file=env_file)

    assert settings.ollama_host.host == "from-file"
    assert settings.log_level == "ERROR"


def test_empty_api_key_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    assert make().anthropic_api_key is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "LOUD"),
        ("OLLAMA_HOST", "not a url"),
        ("OLLAMA_HOST", "ftp://x:21"),
        ("OLLAMA_MODEL", ""),
        ("ANTHROPIC_MODEL", ""),
    ],
)
def test_invalid_values_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        make()


def test_api_key_never_rendered(caplog: pytest.LogCaptureFixture) -> None:
    settings = make(anthropic_api_key=FAKE_KEY)

    caplog.set_level(logging.DEBUG)
    logging.getLogger("aether.test").info("settings: %s / %r", settings, settings)

    rendered = [
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        str(settings.model_dump()),
        caplog.text,
    ]
    for text in rendered:
        assert FAKE_KEY not in text
    assert "**********" in repr(settings)


def test_settings_are_immutable() -> None:
    settings = make()

    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_app_starts_without_api_key() -> None:
    app = create_app(make())

    assert app.state.settings.anthropic_api_key is None
    assert TestClient(app).get("/health").status_code == 200
