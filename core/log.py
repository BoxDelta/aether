"""Structured JSON logging with request/session IDs and secret redaction (docs/05).

Every record becomes one JSON line with: timestamp (UTC), level, logger, message,
request_id, session_id, any ``extra`` fields, and ``exc_info`` when present.

Redaction runs on the final rendered values (message, string extras, traceback
and stack info; bool, number and None extras are kept as they are), in this order:

1. Exact values of configured secrets (``secrets_from()``), longest first:
   - ``ANTHROPIC_API_KEY``;
   - ``AETHER_API_TOKEN``;
   - the password in ``DATABASE_URL``, both as written and percent-decoded.

   Values shorter than ``MIN_SECRET_LENGTH`` (8 characters) are not redacted
   by value, so ordinary log text isn't corrupted; the patterns below still apply.
2. Patterns, configured or not (``_KEY_PATTERNS``):
   - anything that looks like an Anthropic API key (``sk-ant-...``);
   - the credential (8+ characters) in ``Bearer <credential>``, where the word is
     ``Bearer`` or ``bearer`` followed by exactly one space, e.g. a wrong token. Only
     a backstop:
     the auth middleware never logs header values, and the configured token is
     redacted by value.
3. The password in any ``scheme://user:password@host`` URL, configured or not
   (``_URL_CREDENTIALS``; user and host are kept so the log stays useful).

``tests/test_log_redaction_docs.py`` fails if ``secrets_from()`` reads a setting
or ``_KEY_PATTERNS`` gains a pattern that this docstring doesn't list.
"""

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TextIO
from urllib.parse import unquote, urlsplit

from core.config import Settings
from core.request_context import get_request_id, get_session_id

REDACTED = "[REDACTED]"

# Secrets shorter than this are not redacted by value: replacing very short
# strings would corrupt ordinary log text. Real API keys are far longer.
MIN_SECRET_LENGTH = 8

_KEY_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    # Any bearer credential, configured or not (e.g. a wrong token): backstop only,
    # the auth middleware never logs header values (#57).
    re.compile(r"(?<=[Bb]earer )[A-Za-z0-9._~+/=\-]{8,}"),
)

# scheme://user:password@host -> scheme://user:[REDACTED]@host. The password runs
# to the next "@" (or whitespace), so a password containing "/" or ":" is covered;
# when in doubt this over-redacts, which is the safe direction.
_URL_CREDENTIALS = re.compile(r"(?P<head>\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]*:)[^\s@]+@")

# Attributes every LogRecord has; anything else came from ``extra=``.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()
) | {"message", "asctime", "taskName", "color_message"}  # color_message: uvicorn ANSI copy

# Loggers configured by uvicorn's CLI; routed through our handler instead.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

_HANDLER_NAME = "aether-json"

# Third-party loggers that log request/response bodies at DEBUG (the anthropic SDK
# logs the full JSON body, i.e. prompts and the system prompt) or every request
# at INFO. Held at WARNING whatever LOG_LEVEL is, so conversation content never
# reaches AETHER's logs (docs/05 §9).
QUIET_LOGGERS = ("anthropic", "httpx", "httpx2", "httpcore", "ollama")


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Longest first, so a secret containing another is replaced whole.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _KEY_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return _URL_CREDENTIALS.sub(rf"\g<head>{REDACTED}@", text)


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": self._redact(record.getMessage()),
            "request_id": get_request_id(),
            "session_id": get_session_id(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = self._safe_value(value)
        if record.exc_info:
            payload["exc_info"] = self._redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack_info"] = self._redact(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False)

    def _safe_value(self, value: object) -> object:
        if isinstance(value, bool | int | float) or value is None:
            return value
        return self._redact(value if isinstance(value, str) else repr(value))


def secrets_from(settings: Settings) -> list[str]:
    """Secret values from settings that must never appear in logs."""
    secrets: list[str] = []
    if settings.anthropic_api_key is not None:
        secrets.append(settings.anthropic_api_key.get_secret_value())
    if settings.aether_api_token is not None:
        secrets.append(settings.aether_api_token.get_secret_value())
    if settings.database_url is not None:
        password = urlsplit(settings.database_url.get_secret_value()).password
        if password:
            # As written in the URL and percent-decoded (drivers and errors may
            # show either form).
            secrets.extend({password, unquote(password)})
    return secrets


def configure_logging(settings: Settings, stream: TextIO | None = None) -> logging.Handler:
    """Install the JSON handler on the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter(Redactor(secrets_from(settings))))

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    return handler
