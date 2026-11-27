import io
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.app import create_app
from core.config import Settings
from core.log import REDACTED, Redactor, configure_logging
from core.request_context import REQUEST_ID_HEADER, SESSION_ID_HEADER

# Fake keys for tests only.
CONFIGURED_KEY = "sk-ant-test-configured-0123456789"  # gitleaks:allow
OTHER_KEY = "sk-ant-api03-unconfigured-abcdefghij"  # gitleaks:allow

log = logging.getLogger("aether.test")

TEST_TOKEN = "fake-api-token-fixture-0123456789abcdef"  # noqa: S105  # gitleaks:allow
AUTH = {"authorization": f"Bearer {TEST_TOKEN}"}


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.fixture
def stream() -> io.StringIO:
    out = io.StringIO()
    configure_logging(settings(anthropic_api_key=CONFIGURED_KEY), stream=out)
    return out


def test_record_is_one_json_line_with_standard_fields(stream: io.StringIO) -> None:
    log.info("hello %s", "world")

    [record] = lines(stream)
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "aether.test"
    assert record["timestamp"].endswith("+00:00")
    assert record["request_id"] is None
    assert record["session_id"] is None


def test_extra_fields_are_included(stream: io.StringIO) -> None:
    log.info("routed", extra={"provider": "ollama", "latency_ms": 12, "cached": False})

    [record] = lines(stream)
    assert record["provider"] == "ollama"
    assert record["latency_ms"] == 12
    assert record["cached"] is False


def test_configured_secret_is_redacted_everywhere(stream: io.StringIO) -> None:
    log.info("key=%s", CONFIGURED_KEY, extra={"header": f"x-api-key: {CONFIGURED_KEY}"})
    try:
        raise ValueError(f"bad key {CONFIGURED_KEY}")
    except ValueError:
        log.exception("failed")

    output = stream.getvalue()
    assert CONFIGURED_KEY not in output
    first, second = lines(stream)
    assert first["message"] == f"key={REDACTED}"
    assert first["header"] == f"x-api-key: {REDACTED}"
    assert f"bad key {REDACTED}" in second["exc_info"]


def test_unconfigured_anthropic_key_shape_is_redacted(stream: io.StringIO) -> None:
    log.warning("leaked %s", OTHER_KEY)

    assert OTHER_KEY not in stream.getvalue()
    assert lines(stream)[0]["message"] == f"leaked {REDACTED}"


def test_non_string_extra_is_rendered_and_redacted(stream: io.StringIO) -> None:
    log.info("obj", extra={"payload": {"key": CONFIGURED_KEY}})

    assert CONFIGURED_KEY not in stream.getvalue()


def test_short_secrets_are_not_used_for_redaction() -> None:
    redact = Redactor(["abc", "a-long-enough-secret"])

    assert redact("abc and a-long-enough-secret") == f"abc and {REDACTED}"


def test_log_level_from_settings_is_applied() -> None:
    out = io.StringIO()
    configure_logging(settings(log_level="WARNING"), stream=out)

    log.info("hidden")
    log.warning("shown")

    assert [r["message"] for r in lines(out)] == ["shown"]


def test_configure_logging_is_idempotent() -> None:
    configure_logging(settings(), stream=io.StringIO())
    configure_logging(settings(), stream=io.StringIO())

    ours = [h for h in logging.getLogger().handlers if h.get_name() == "aether-json"]
    assert len(ours) == 1


def test_uvicorn_loggers_propagate_to_json_handler() -> None:
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.addHandler(logging.StreamHandler(io.StringIO()))
    uvicorn_access.propagate = False
    out = io.StringIO()

    configure_logging(settings(), stream=out)
    uvicorn_access.info("GET /health 200")

    assert uvicorn_access.handlers == []
    assert lines(out)[0]["logger"] == "uvicorn.access"


def test_uvicorn_color_message_is_dropped(stream: io.StringIO) -> None:
    logging.getLogger("uvicorn.error").info("Started", extra={"color_message": "\x1b[36mStarted"})

    assert "color_message" not in lines(stream)[0]


@pytest.fixture
def app_and_stream() -> tuple[FastAPI, io.StringIO]:
    app = create_app(settings(anthropic_api_key=CONFIGURED_KEY, aether_api_token=TEST_TOKEN))
    out = io.StringIO()
    # create_app installed the stdout handler; point it at a buffer for the test.
    configure_logging(app.state.settings, stream=out)

    @app.get("/_test/log")
    async def emit() -> dict[str, bool]:
        log.info("inside request")
        return {"ok": True}

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError(f"failed with {CONFIGURED_KEY}")

    @app.get("/_test/values")
    async def values() -> None:
        record_text = "-".join(("RECORD", "VALUE", "CANARY", "7731"))  # built at run time
        raise ValueError(record_text)

    return app, out


def test_ids_appear_in_logs_inside_a_request(app_and_stream: tuple[FastAPI, io.StringIO]) -> None:
    app, out = app_and_stream

    TestClient(app, headers=AUTH).get(
        "/_test/log", headers={REQUEST_ID_HEADER: "req-7", SESSION_ID_HEADER: "s-7"}
    )

    [record] = [r for r in lines(out) if r["message"] == "inside request"]
    assert record["request_id"] == "req-7"
    assert record["session_id"] == "s-7"


def test_unhandled_exception_logged_with_ids_and_redacted(
    app_and_stream: tuple[FastAPI, io.StringIO],
) -> None:
    app, out = app_and_stream

    TestClient(app, headers=AUTH, raise_server_exceptions=False).get(
        "/_test/boom", headers={REQUEST_ID_HEADER: "req-500"}
    )

    [record] = [r for r in lines(out) if r["message"] == "Unhandled exception"]
    assert record["level"] == "ERROR"
    assert record["request_id"] == "req-500"
    assert record["path"] == "/_test/boom"
    assert record["error_type"] == "RuntimeError"
    assert "exc_info" not in record  # no message: it can hold values (#124)
    assert "boom" in record["frames"]  # where it happened is kept
    assert CONFIGURED_KEY not in out.getvalue()


def test_unhandled_exception_message_is_never_logged(
    app_and_stream: tuple[FastAPI, io.StringIO], capsys: pytest.CaptureFixture[str]
) -> None:
    # A message can hold request or response values (a ResponseValidationError
    # quotes them): only the type and frames are logged (#124).
    app, out = app_and_stream
    response = TestClient(app, headers=AUTH, raise_server_exceptions=False).get("/_test/values")

    assert response.status_code == 500
    [record] = [r for r in lines(out) if r["message"] == "Unhandled exception"]
    assert record["error_type"] == "ValueError"
    for text in (out.getvalue(), response.text, *capsys.readouterr()):
        assert "RECORD-VALUE-CANARY-7731" not in text


def test_third_party_loggers_stay_quiet_at_debug() -> None:
    # The anthropic SDK logs full request bodies (prompts) at DEBUG.
    configure_logging(settings(log_level="DEBUG"), stream=io.StringIO())
    assert logging.getLogger().level == logging.DEBUG
    for name in ("anthropic", "httpx", "httpx2", "httpcore", "ollama"):
        assert logging.getLogger(name).level == logging.WARNING
