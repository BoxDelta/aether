"""Core configuration, read from environment variables and an optional ``.env`` file.

Environment variables take precedence over ``.env``. Secrets are ``SecretStr`` so
they never appear in ``repr``/``str``/JSON dumps or in logs (docs/05).
"""

import re
from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# SQLAlchemy async engine with the asyncpg driver (ADR-020).
DATABASE_SCHEME = "postgresql+asyncpg"

# Output token cap for cloud model calls (#77, Charter cost awareness). Thinking
# tokens count toward it. AETHER_CLOUD_MAX_OUTPUT_TOKENS overrides the default.
DEFAULT_CLOUD_MAX_OUTPUT_TOKENS = 4096
CLOUD_MAX_OUTPUT_TOKENS_LIMIT = 32_000
CLOUD_MAX_OUTPUT_TOKENS_ERROR = (
    "AETHER_CLOUD_MAX_OUTPUT_TOKENS must be a whole number from 1 to "
    f"{CLOUD_MAX_OUTPUT_TOKENS_LIMIT} (unset means {DEFAULT_CLOUD_MAX_OUTPUT_TOKENS})"
)
_WHOLE_NUMBER = re.compile(r"[0-9]+")


class ConfigError(ValueError):
    """A setting is invalid. The message names the setting, never its value."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Optional until a real key is configured; the Claude adapter (#15) reports
    # itself unavailable when this is unset.
    anthropic_api_key: SecretStr | None = None
    # Default cloud model (Claude API).
    anthropic_model: str = Field(default="claude-opus-5-5", min_length=1)
    ollama_host: AnyHttpUrl = AnyHttpUrl("http://localhost:11434")
    # Opt-in (#10, ADR-023): let Ollama receive Sensitive data on a host that is not
    # loopback or a private/link-local IP literal, e.g. a private overlay network.
    # Off by default; without it such a host is capped at "personal".
    aether_ollama_trust_host: bool = False
    # Default local model. Must fit the VRAM budget (Charter: keep >= 1.5 GB of 8 GB free);
    # confirmed on the real PC in the Phase 1 exit check (#23).
    ollama_model: str = Field(default="llama3.1:8b", min_length=1)
    log_level: LogLevel = "INFO"
    # Postgres (ADR-011, ADR-020): postgresql+asyncpg://user:password@host:port/db.
    # Secret because it carries the password; optional until the database is set up.
    # Checked in core.db, not here: a pydantic validation error keeps the raw input
    # in .errors()/.json() (even with hide_input_in_errors), which would expose
    # the password to anything that logs it.
    database_url: SecretStr | None = None
    # The owner role, for Alembic migrations only (#1, #12). Core never connects
    # with it; it is loaded here so the log redactor knows its password. When
    # unset, migrations fall back to DATABASE_URL (a throwaway CI database).
    database_migration_url: SecretStr | None = None

    # Folder for database backups (#3, core.backup). Default:
    # ~/.local/share/aether/backups, outside the repository. Kept as text and
    # checked in core.backup, so an error names the setting, not the value.
    aether_backup_dir: str | None = None

    # Bearer token for every route except /health (ADR-021, #57). Without it Core
    # serves only /health. Length is checked in core.auth, not here (see above).
    aether_api_token: SecretStr | None = None

    # Output token cap for cloud calls only (#77). Kept as raw text here and parsed
    # in ``cloud_max_output_tokens``: a pydantic error would echo the bad value.
    aether_cloud_max_output_tokens: str | None = None

    @property
    def cloud_max_output_tokens(self) -> int:
        """The cloud output token cap. Raises ``ConfigError`` (value not shown)."""
        raw = self.aether_cloud_max_output_tokens
        if raw is None:
            return DEFAULT_CLOUD_MAX_OUTPUT_TOKENS
        text = raw.strip()
        if not _WHOLE_NUMBER.fullmatch(text):
            raise ConfigError(CLOUD_MAX_OUTPUT_TOKENS_ERROR)
        # Python refuses to convert more than 4,300 digits (a ValueError, which would
        # skip our message): longer than the limit's digits is out of range anyway.
        digits = text.lstrip("0") or "0"
        if len(digits) > len(str(CLOUD_MAX_OUTPUT_TOKENS_LIMIT)):
            raise ConfigError(CLOUD_MAX_OUTPUT_TOKENS_ERROR)
        value = int(digits)
        if not 1 <= value <= CLOUD_MAX_OUTPUT_TOKENS_LIMIT:
            raise ConfigError(CLOUD_MAX_OUTPUT_TOKENS_ERROR)
        return value

    @field_validator(
        "anthropic_api_key",
        "database_url",
        "database_migration_url",
        "aether_api_token",
        "aether_cloud_max_output_tokens",
        "aether_backup_dir",
        mode="before",
    )
    @classmethod
    def _empty_key_is_unset(cls, value: object) -> object:
        # `ANTHROPIC_API_KEY=` etc. left empty (as in .env.example) mean "not configured".
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str) and not raw.strip():
            return None
        return value

    @field_validator("aether_ollama_trust_host", mode="before")
    @classmethod
    def _empty_flag_is_off(cls, value: object) -> object:
        # `AETHER_OLLAMA_TRUST_HOST=` left empty (as in .env.example) means off.
        if isinstance(value, str) and not value.strip():
            return False
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, loaded once."""
    return Settings()
