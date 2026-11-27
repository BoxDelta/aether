"""Dispatcher settings: env file only, mode 600, loopback LiteLLM, no secret in errors."""

from collections.abc import Callable
from pathlib import Path

import pytest

from dispatch.config import ConfigError, load_settings
from tests.dispatch.helpers import OWNER, fake_value, fake_webhook, write_env


def test_loads_from_file(tmp_path: Path) -> None:
    token = fake_value("gh")
    settings = load_settings(write_env(tmp_path / "d.env", DISPATCH_GITHUB_TOKEN=token))
    assert settings.dispatch_owner == OWNER
    assert settings.dispatch_github_token.get_secret_value() == token
    assert token not in repr(settings)


def test_default_path_from_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_env(tmp_path / "elsewhere.env")
    monkeypatch.setenv("DISPATCH_ENV_FILE", str(path))
    assert load_settings().dispatch_repo == "aether"


def test_process_environment_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_OWNER", "someone-else")
    assert load_settings(write_env(tmp_path / "d.env")).dispatch_owner == OWNER


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.env")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_group_or_other_readable_file_is_refused(tmp_path: Path, mode: int) -> None:
    path = write_env(tmp_path / "d.env")
    path.chmod(mode)
    with pytest.raises(ConfigError, match="mode 600"):
        load_settings(path)


def test_missing_value_names_field_not_value(tmp_path: Path) -> None:
    path = write_env(tmp_path / "d.env", DISPATCH_GITHUB_TOKEN="", DISPATCH_LITELLM_KEY="fake-k-9")
    with pytest.raises(ConfigError) as info:
        load_settings(path)
    assert "dispatch_github_token" in str(info.value)
    assert "fake-k-9" not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:4000",
        "https://litellm.example.com",
        "http://user:pw@127.0.0.1:4000",
        "ftp://127.0.0.1",
        "http://127.0.0.1:4000?x=1",
    ],
)
def test_litellm_url_must_be_plain_loopback(tmp_path: Path, url: str) -> None:
    with pytest.raises(ConfigError, match="dispatch_litellm_url"):
        load_settings(write_env(tmp_path / "d.env", DISPATCH_LITELLM_URL=url))


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:4000/", "http://localhost:4000", "http://[::1]:4000"]
)
def test_loopback_urls_accepted(tmp_path: Path, url: str) -> None:
    settings = load_settings(write_env(tmp_path / "d.env", DISPATCH_LITELLM_URL=url))
    assert not settings.dispatch_litellm_url.endswith("/")


@pytest.mark.parametrize("name", ["../etc", "a/b", "x?y", "-lead"])
def test_owner_and_repo_are_plain_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="dispatch_repo"):
        load_settings(write_env(tmp_path / "d.env", DISPATCH_REPO=name))


def test_webhook_is_optional(tmp_path: Path) -> None:
    settings = load_settings(write_env(tmp_path / "d.env", DISPATCH_DISCORD_WEBHOOK=""))
    assert settings.dispatch_discord_webhook is None


def test_webhook_loaded_as_secret(tmp_path: Path) -> None:
    url = fake_webhook()
    settings = load_settings(write_env(tmp_path / "d.env", DISPATCH_DISCORD_WEBHOOK=url))
    assert settings.dispatch_discord_webhook is not None
    assert settings.dispatch_discord_webhook.get_secret_value() == url
    assert url not in repr(settings)


@pytest.mark.parametrize(
    "transform",
    [
        lambda u: u.replace("https://", "http://"),
        lambda u: u.replace("discord.com", "discordapp.com"),
        lambda u: u.replace("discord.com", "discord.com.evil.example"),
        lambda u: u.replace("discord.com", "evil.example/discord.com"),
        lambda u: u.replace("discord.com", "canary.discord.com"),
        lambda u: u.replace("discord.com", "discord.com:8443"),
        lambda u: u.replace("discord.com", "user@discord.com"),
        lambda u: u.replace("discord.com", "DISCORD.com"),
        lambda u: u.replace("/api/webhooks/", "/api/v9/channels/"),
        lambda u: u.split("/api/")[0] + "/api/webhooks/",
        lambda u: u + "?wait=true",
        lambda u: u + "#x",
    ],
)
def test_webhook_validation(tmp_path: Path, transform: Callable[[str], str]) -> None:
    url = transform(fake_webhook())
    with pytest.raises(ConfigError) as info:
        load_settings(write_env(tmp_path / "d.env", DISPATCH_DISCORD_WEBHOOK=url))
    assert "dispatch_discord_webhook" in str(info.value)
    assert url not in str(info.value)
