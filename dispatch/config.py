"""Dispatcher settings (ADR-030 §Credentials).

Read only from the env file named by ``DISPATCH_ENV_FILE`` (default
``~/.config/aether-dispatch/dispatch.env``), never from the process environment,
so a stray shell variable cannot redirect the token. The file must not be
readable by group or others. Secret values are ``SecretStr`` and never printed.
"""

import ipaddress
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_ENV_FILE = Path("~/.config/aether-dispatch/dispatch.env")
ENV_FILE_VAR = "DISPATCH_ENV_FILE"

# GitHub user and repository names: letters, digits, '-', '_', '.'.
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_LOOPBACK_NAMES = frozenset({"localhost"})


class ConfigError(Exception):
    """Settings are missing or unsafe. The message never contains a secret value."""


class DispatchSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    dispatch_github_token: SecretStr = Field(min_length=1)
    dispatch_litellm_key: SecretStr = Field(min_length=1)
    dispatch_litellm_url: str
    dispatch_owner: str
    dispatch_repo: str

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],  # noqa: ARG003 - fixed pydantic-settings signature
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The env file only: process environment variables are ignored on purpose.
        return (init_settings, dotenv_settings)

    @field_validator("dispatch_owner", "dispatch_repo")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("must be a plain GitHub user or repository name")
        return value

    @field_validator("dispatch_litellm_url")
    @classmethod
    def _loopback_url(cls, value: str) -> str:
        # LiteLLM is bound to 127.0.0.1 (ADR-030 §Scope); its key never leaves the PC.
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("must be an http(s) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("must not carry credentials, a query or a fragment")
        if not _is_loopback(parts.hostname):
            raise ValueError("must point at a loopback address")
        return value.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def env_file_path() -> Path:
    raw = os.environ.get(ENV_FILE_VAR)
    return (Path(raw) if raw else DEFAULT_ENV_FILE).expanduser()


def load_settings(path: Path | None = None) -> DispatchSettings:
    """Load settings from ``path`` (default: ``env_file_path()``).

    Raises ``ConfigError`` if the file is missing, readable by group or others,
    or has a missing or invalid value. Errors name fields, never values.
    """
    env_file = path or env_file_path()
    try:
        mode = env_file.stat().st_mode
    except OSError as exc:
        raise ConfigError(f"env file not found: {env_file}") from exc
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"env file must be mode 600: {env_file}")
    try:
        return DispatchSettings(_env_file=env_file)
    except ValueError as exc:
        fields = sorted({str(err["loc"][0]) for err in _errors(exc) if err.get("loc")})
        raise ConfigError(f"invalid or missing settings: {', '.join(fields)}") from None


def _errors(exc: ValueError) -> list[dict[str, tuple[object, ...]]]:
    errors = getattr(exc, "errors", None)
    return list(errors(include_input=False)) if callable(errors) else []
