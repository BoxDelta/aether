import io
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.app import create_app
from core.config import Settings
from core.log import (
    REDACTED,
    AccessLogQueryFilter,
    Redactor,
    SafeStreamHandler,
    configure_logging,
)
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


# --- the handler's own error path (#114) ----------------------------------------------


class _BrokenRepr:
    def __repr__(self) -> str:
        raise RuntimeError(f"repr failed near {CONFIGURED_KEY}")


@pytest.fixture
def handler() -> tuple[logging.Handler, io.StringIO]:
    """AETHER's handler, called directly: pytest's own capture handlers re-raise
    logging errors, so these records must not go through a logger."""
    out = io.StringIO()
    return configure_logging(settings(anthropic_api_key=CONFIGURED_KEY), stream=out), out


def record(msg: str, args: tuple[object, ...], **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("aether.test", logging.INFO, __file__, 1, msg, args, None)
    rec.__dict__.update(extra)
    return rec


@pytest.mark.parametrize(
    ("rec", "error_type"),
    [
        # One argument too few: getMessage() raises TypeError with the args in hand.
        (lambda: record("token %s and %s", (CONFIGURED_KEY,)), "TypeError"),
        (lambda: record("value", (), thing=_BrokenRepr()), "RuntimeError"),
    ],
)
def test_a_record_that_fails_to_format_writes_one_fixed_line_to_stderr(
    handler: tuple[logging.Handler, io.StringIO],
    capsys: pytest.CaptureFixture[str],
    rec: Any,
    error_type: str,
) -> None:
    h, out = handler
    h.handle(rec())

    err = capsys.readouterr().err
    assert CONFIGURED_KEY not in err
    assert "token" not in err
    assert "Traceback" not in err
    [line] = [json.loads(line) for line in err.splitlines()]
    assert line == {
        "level": "ERROR",
        "logger": "aether.log",
        "message": "Log record could not be written",
        "error_type": error_type,
        # Where the broken call was: code locations, not data.
        "source_logger": "aether.test",
        "source_file": __file__,
        "source_line": 1,
        "source_function": None,
    }
    assert out.getvalue() == ""  # nothing half-written to the log itself


def test_a_write_to_a_closed_stream_writes_one_fixed_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = io.StringIO()
    h = configure_logging(settings(anthropic_api_key=CONFIGURED_KEY), stream=out)
    out.close()
    h.handle(record("token %s", (CONFIGURED_KEY,)))

    err = capsys.readouterr().err
    assert CONFIGURED_KEY not in err and "Traceback" not in err
    [line] = [json.loads(line) for line in err.splitlines()]
    assert line["error_type"] == "ValueError"


def test_logging_still_works_after_a_failed_record(
    handler: tuple[logging.Handler, io.StringIO], capsys: pytest.CaptureFixture[str]
) -> None:
    h, out = handler
    h.handle(record("broken %s %s", ("one",)))
    h.handle(record("fine", ()))
    capsys.readouterr()
    assert [line["message"] for line in lines(out)] == ["fine"]


def test_configure_logging_installs_the_safe_handler() -> None:
    assert isinstance(configure_logging(settings(), stream=io.StringIO()), SafeStreamHandler)


# --- memory query strings stay out of the access log (#93) -----------------------------


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/memory?key=canary.secret_key", "/memory?<query removed>"),
        ("/memory?query=my+private+phrase", "/memory?<query removed>"),
        ("/memory/export?privacy=public", "/memory/export?<query removed>"),
        ("/memory/0b9e4c1e?x=1", "/memory/0b9e4c1e?<query removed>"),
        ("/memory", "/memory"),
        ("/chat?debug=1", "/chat?debug=1"),  # other paths unchanged
        ("/memoryless?a=1", "/memoryless?a=1"),
    ],
)
def test_access_log_drops_memory_query_strings(stream: io.StringIO, path: str, logged: str) -> None:
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)
    # The exact call uvicorn makes (uvicorn/protocols/http/h11_impl.py).
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 422)
    [record] = [r for r in lines(stream) if r["logger"] == "uvicorn.access"]
    assert record["message"] == f'127.0.0.1:5555 - "GET {logged} HTTP/1.1" 422'
    assert "canary" not in stream.getvalue() and "private" not in stream.getvalue()


def test_the_access_log_filter_is_installed_once() -> None:
    for _ in range(3):
        configure_logging(settings(), stream=io.StringIO())
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, AccessLogQueryFilter) for f in access.filters) == 1


def test_access_log_filter_works_after_uvicorns_own_logging_setup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The real order with `uvicorn core.app:create_app --factory`: uvicorn's
    # dictConfig first, then create_app() configures AETHER's logging (#93).
    import uvicorn

    from core.identity import load_identity

    uvicorn.Config("core.app:create_app", factory=True).configure_logging()
    create_app(settings(), load_identity())
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)
    capsys.readouterr()
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/memory?key=canary_k", "1.1", 422)
    out = capsys.readouterr().out
    assert "/memory?<query removed>" in out
    assert "canary_k" not in out
