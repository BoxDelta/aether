"""POST /chat end to end (#22) with fake providers and an in-memory event store."""

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.app import create_app
from core.chat import MAX_BODY_BYTES, MAX_MESSAGE_CHARS, MAX_MESSAGES, MAX_TOTAL_CHARS
from core.config import Settings
from core.events import UNEXPECTED_ERROR_REASON, Event, EventWriter, request_crashed
from core.identity import load_identity
from core.request_context import REQUEST_ID_HEADER
from router.contract import (
    Message as ModelMessage,
)
from router.contract import (
    ModelRequest,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from router.routing import RouteOptions, Router
from tests.chat_helpers import (
    ANSWER,
    AUTH,
    FAKE_KEY,
    LOCAL,
    PROMPT,
    TOKEN,
    USAGE,
    Setup,
    assert_no_content,
    error_with_key_cause,
)
from tests.fakes import CHECK, FakeProvider, snapshot


class FailingWriter(EventWriter):
    def __init__(self, error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.error = error
        self.delay_s = delay_s

    async def write_all(
        self,
        events: Sequence[Event],
        *,
        request: ModelRequest | None = None,
        also_forbidden: Sequence[str] = (),
    ) -> list[int]:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return []


# --- success paths -------------------------------------------------------------------


def test_local_route_end_to_end() -> None:
    s = Setup()

    r = s.post()

    assert r.status_code == 200
    body = r.json()
    assert body["text"] == ANSWER
    assert (body["provider"], body["provider_kind"], body["model"]) == (
        "ollama",
        "local",
        "ollama-model",
    )
    assert body["route_reason"].startswith("local preferred")
    assert body["failover_from"] is None
    assert (body["input_tokens"], body["output_tokens"]) == (11, 7)
    assert body["request_id"] == r.headers[REQUEST_ID_HEADER]
    # identity was applied (call_with_identity)
    assert s.local.requests[0].system is not None
    assert s.cloud.requests == []

    assert [e["event_type"] for e in s.rows] == [
        "request.received",
        "model.route.selected",
        "model.call.completed",
    ]
    assert {e["request_id"] for e in s.rows} == {body["request_id"]}
    completed = s.rows[-1]
    assert (completed["input_tokens"], completed["output_tokens"]) == (11, 7)
    assert completed["latency_ms"] >= 0
    assert_no_content(repr(s.rows))


def test_cloud_route_when_allowed_and_no_vram() -> None:
    s = Setup(free_gib=0.5)

    r = s.post(allow_cloud=True)

    assert r.status_code == 200
    assert r.json()["provider"] == "claude"
    assert s.local.requests == []
    selected = [e for e in s.rows if e["event_type"] == "model.route.selected"]
    assert [(e["provider"], e["allow_cloud"]) for e in selected] == [("claude", True)]


def test_defaults_are_personal_and_local_only() -> None:
    s = Setup(free_gib=0.5)  # no VRAM headroom: cloud would be chosen if allowed

    r = s.post()  # no privacy, no allow_cloud

    assert r.status_code == 200
    assert r.json()["provider"] == "ollama"
    assert s.local.requests[0].privacy == "personal"
    assert s.cloud.requests == []
    received = s.rows[0]
    assert (received["privacy"], received["allow_cloud"]) == ("personal", False)


def test_failover_success_reports_and_records_both_decisions() -> None:
    s = Setup(local_error=ProviderTimeout("ollama", "Ollama did not respond in time"))

    r = s.post(allow_cloud=True)

    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "claude"
    assert body["failover_from"] == {
        "provider": "ollama",
        "error_type": "ProviderTimeout",
        "message": "Ollama did not respond in time",
    }
    assert [(e["event_type"], e["provider"]) for e in s.rows] == [
        ("request.received", None),
        ("model.route.selected", "ollama"),
        ("provider.unavailable", "ollama"),
        ("model.route.selected", "claude"),
        ("model.call.completed", "claude"),
    ]


def test_refusal_is_returned_as_answer() -> None:
    s = Setup(finish_reason="refusal")

    r = s.post()

    assert r.status_code == 200
    assert (r.json()["text"], r.json()["finish_reason"]) == ("", "refusal")
    assert s.rows[-1]["outcome"] == "refusal"
    assert s.cloud.requests == []


# --- failure paths -------------------------------------------------------------------


def test_route_failed_body_has_only_safe_fields() -> None:
    # Issue acceptance: __cause__ carries a fake key and traceback text.
    s = Setup(
        local_error=error_with_key_cause("ollama"), cloud_error=error_with_key_cause("claude")
    )

    r = s.post(allow_cloud=True)

    assert r.status_code == 502
    body = r.json()
    assert body == {
        "request_id": r.headers[REQUEST_ID_HEADER],
        "error": {
            "type": "RouteFailed",
            "message": "fallback claude also failed (ProviderUnavailable)",
            "attempts": [
                {
                    "provider": "ollama",
                    "error_type": "ProviderUnavailable",
                    "message": "cannot connect to ollama",
                },
                {
                    "provider": "claude",
                    "error_type": "ProviderUnavailable",
                    "message": "cannot connect to claude",
                },
            ],
        },
    }
    assert_no_content(r.text)

    types = [e["event_type"] for e in s.rows]
    assert types[0] == "request.received"
    assert types.count("model.route.selected") == 2
    assert types.count("provider.unavailable") == 2
    assert types[-1] == "request.failed"
    failed = s.rows[-1]
    assert [a["provider"] for a in failed["attempts"]] == ["ollama", "claude"]
    assert_no_content(repr(s.rows))


def test_single_failure_without_cloud_is_route_failed() -> None:
    s = Setup(local_error=error_with_key_cause("ollama"))

    r = s.post()  # allow_cloud defaults to false: no failover

    assert r.status_code == 502
    error = r.json()["error"]
    assert error["message"] == (
        "no failover: no provider: local unavailable; cloud not allowed for this request"
    )
    assert [a["provider"] for a in error["attempts"]] == ["ollama"]
    assert s.cloud.requests == []
    assert_no_content(r.text)


def test_no_route_for_secret() -> None:
    s = Setup()

    r = s.post(privacy="secret", allow_cloud=True)

    assert r.status_code == 503
    assert r.json()["error"] == {
        "type": "NoRoute",
        "message": "secret data is never sent to any model",
        "attempts": [],
    }
    assert s.local.requests == [] and s.cloud.requests == []
    assert [e["event_type"] for e in s.rows] == ["request.received", "request.failed"]
    assert s.rows[0]["privacy"] == "secret"


def test_unhandled_error_is_generic() -> None:
    s = Setup(local_error=RuntimeError(f"boom {FAKE_KEY} {PROMPT}"))

    # Provider.generate turns a raw exception into ProviderBadResponse (type name only).
    r = s.post()

    assert r.status_code == 502
    assert r.json()["error"]["attempts"][0] == {
        "provider": "ollama",
        "error_type": "ProviderBadResponse",
        "message": "unexpected RuntimeError from adapter",
    }
    assert_no_content(r.text)


def test_unexpected_server_error_returns_no_details() -> None:
    s = Setup()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"bug with {FAKE_KEY}")

    s.client.app.state.router.handle = broken  # type: ignore[attr-defined]
    r = s.post()

    assert r.status_code == 500
    # Generic body from RequestContextMiddleware (#7): no exception text.
    assert r.json() == {"detail": "Internal Server Error"}
    assert r.headers[REQUEST_ID_HEADER]
    assert_no_content(r.text)
    assert FAKE_KEY not in s.log.getvalue()  # logged traceback is redacted


# --- events must never break a request ----------------------------------------------


@pytest.mark.parametrize(
    ("writer", "error_type"),
    [
        (
            FailingWriter(ConnectionRefusedError(111, f"refused {FAKE_KEY}")),
            "ConnectionRefusedError",
        ),
        (FailingWriter(delay_s=5.0), "TimeoutError"),
    ],
    ids=["db-error", "db-timeout"],
)
def test_event_write_failure_still_answers(writer: EventWriter, error_type: str) -> None:
    s = Setup(writer=writer, recorder_timeout_s=0.1)

    r = s.post()

    assert r.status_code == 200
    assert r.json()["text"] == ANSWER
    warnings = [x for x in s.log_records() if x["message"] == "Event write failed"]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "WARNING"
    assert warnings[0]["error_type"] == error_type
    assert "refused" not in json.dumps(warnings[0])  # error type only, no message
    assert "exc_info" not in warnings[0]


def test_no_database_url_skips_events_with_one_startup_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(Settings(_env_file=None))
    out = capsys.readouterr().out
    warnings = [json.loads(line) for line in out.splitlines() if "Events disabled" in line]
    assert [w["message"] for w in warnings] == ["Events disabled: DATABASE_URL is not set"]
    assert app.state.events.enabled is False


def test_malformed_database_url_skips_events_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "mysql://aether:not-a-real-pw-123@host/db"
    app = create_app(Settings(_env_file=None, database_url=SecretStr(url)))
    out = capsys.readouterr().out
    assert "Events disabled: DATABASE_URL must start with postgresql+asyncpg://" in out
    assert "not-a-real-pw-123" not in out
    assert app.state.events.enabled is False


# --- request limits and validation ---------------------------------------------------


def test_body_over_limit_is_413_before_parsing() -> None:
    s = Setup()
    big = "x" * (MAX_BODY_BYTES + 1)

    r = s.client.post("/chat", content=big, headers={"content-type": "application/json"})

    assert r.status_code == 413
    assert r.json()["error"]["type"] == "RequestTooLarge"
    assert r.json()["request_id"] == r.headers[REQUEST_ID_HEADER]
    assert s.local.requests == [] and s.rows == []


def test_chunked_body_over_limit_is_413() -> None:
    s = Setup()

    def chunks() -> Any:
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    r = s.client.post("/chat", content=chunks(), headers={"content-type": "application/json"})

    assert r.status_code == 413


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": []},
        {"messages": [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)},
        {"messages": [{"role": "user", "content": "x" * (MAX_MESSAGE_CHARS + 1)}]},
        {
            "messages": [{"role": "user", "content": "x" * MAX_MESSAGE_CHARS}] * 3
            + [{"role": "user", "content": "x" * (MAX_TOTAL_CHARS - 3 * MAX_MESSAGE_CHARS + 1)}]
        },
        {"messages": [{"role": "system", "content": PROMPT}]},
        {"messages": [{"role": "user", "content": PROMPT}], "model": "anything"},
        {"messages": [{"role": "user", "content": PROMPT}], "privacy": "top-secret"},
        {"messages": [{"role": "user", "content": PROMPT}], "override": "Not Valid!"},
    ],
    ids=[
        "empty",
        "too-many",
        "too-long",
        "total-too-long",
        "bad-role",
        "extra-field",
        "bad-privacy",
        "bad-override",
    ],
)
def test_invalid_request_is_422_without_echo(payload: dict[str, Any]) -> None:
    s = Setup()

    r = s.client.post("/chat", json=payload)

    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "ValidationError"
    assert body["request_id"] == r.headers[REQUEST_ID_HEADER]
    assert PROMPT not in r.text and "xxxxxxxxxx" not in r.text
    assert s.local.requests == [] and s.rows == []


def test_malformed_json_is_422() -> None:
    s = Setup()
    r = s.client.post(
        "/chat", content=b'{"messages": [', headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "ValidationError"


# --- access ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "172.18.0.1", "100.64.0.7"])
def test_any_client_address_with_the_token_is_served(host: str) -> None:
    # The interim loopback check is gone (ADR-021 addendum, #57): the token decides.
    # 172.18.0.1 is how n8n's Docker network reaches the host.
    assert Setup(client=(host, 1234)).post().status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "172.18.0.1"])
def test_no_token_is_refused_from_any_address(host: str) -> None:
    s = Setup(client=(host, 1234))
    payload = {"messages": [{"role": "user", "content": PROMPT}]}

    r = s.client.post("/chat", json=payload, headers={"authorization": ""})

    assert r.status_code == 401
    assert s.local.requests == [] and s.rows == []


# --- logs -----------------------------------------------------------------------------


def test_request_logs_contain_no_content() -> None:
    s = Setup(local_error=ProviderTimeout("ollama", "slow"))

    s.post(allow_cloud=True)

    text = s.log.getvalue()
    assert "Route chosen" in text and "Failover" in text and "Model call" in text
    assert_no_content(text)


def test_provider_error_type_is_provider_error() -> None:
    assert issubclass(ProviderUnavailable, ProviderError)  # guard for the fixtures above


def test_real_writer_with_unreachable_database_still_answers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Valid URL, nothing listening on port 1: the real asyncpg writer fails to connect.
    url = "postgresql+asyncpg://aether:not-a-real-pw-456@127.0.0.1:1/aether"
    identity = load_identity()
    local = FakeProvider("ollama", "local", answer=ANSWER, usage=USAGE)
    router = Router([local], identity, secret_check=CHECK, probe=lambda: snapshot(4.0))
    app = create_app(
        Settings(_env_file=None, database_url=SecretStr(url), aether_api_token=SecretStr(TOKEN)),
        identity,
        router=router,
    )
    assert app.state.events.enabled is True

    with TestClient(
        app, client=LOCAL, headers=AUTH
    ) as client:  # runs startup and shutdown (engine dispose)
        r = client.post("/chat", json={"messages": [{"role": "user", "content": PROMPT}]})

    assert r.status_code == 200
    assert r.json()["text"] == ANSWER
    out = capsys.readouterr().out
    warnings = [json.loads(x) for x in out.splitlines() if "Event write failed" in x]
    assert len(warnings) == 1
    assert warnings[0]["error_type"] in {"OSError", "ConnectionRefusedError"}
    assert "not-a-real-pw-456" not in out
    assert PROMPT not in out and ANSWER not in out


# --- event order (#30): appended as things happen, one order for every path ---------

EVENT_ORDERS = [
    pytest.param(
        {},
        {},
        200,
        [
            ("request.received", None),
            ("model.route.selected", "ollama"),
            ("model.call.completed", "ollama"),
        ],
        id="no-failover",
    ),
    pytest.param(
        {"local_error": ProviderTimeout("ollama", "slow")},
        {"allow_cloud": True},
        200,
        [
            ("request.received", None),
            ("model.route.selected", "ollama"),
            ("provider.unavailable", "ollama"),
            ("model.route.selected", "claude"),
            ("model.call.completed", "claude"),
        ],
        id="failover-succeeds",
    ),
    pytest.param(
        {
            "local_error": error_with_key_cause("ollama"),
            "cloud_error": error_with_key_cause("claude"),
        },
        {"allow_cloud": True},
        502,
        [
            ("request.received", None),
            ("model.route.selected", "ollama"),
            ("provider.unavailable", "ollama"),
            ("model.route.selected", "claude"),
            ("provider.unavailable", "claude"),
            ("request.failed", None),
        ],
        id="failover-fails",
    ),
    pytest.param(
        {"local_error": error_with_key_cause("ollama")},
        {},  # allow_cloud defaults to false: no fallback
        502,
        [
            ("request.received", None),
            ("model.route.selected", "ollama"),
            ("provider.unavailable", "ollama"),
            ("request.failed", None),
        ],
        id="no-fallback",
    ),
]


@pytest.mark.parametrize(("setup", "body", "status", "expected"), EVENT_ORDERS)
def test_events_are_recorded_in_the_order_they_happen(
    setup: dict[str, Any], body: dict[str, Any], status: int, expected: list[tuple[str, Any]]
) -> None:
    s = Setup(**setup)

    r = s.post(**body)

    assert r.status_code == status
    assert [(e["event_type"], e["provider"]) for e in s.rows] == expected
    # Each occurred_at is taken when the thing happened, so they never go backwards.
    times = [e["occurred_at"] for e in s.rows]
    assert times == sorted(times)
    assert_no_content(repr(s.rows))


def test_an_unexpected_error_still_records_its_events(monkeypatch: pytest.MonkeyPatch) -> None:
    # A router or adapter bug (#112): background tasks don't run when the handler
    # raises, so the events are written before the generic 500.
    s = Setup()
    router = s.client.app.state.router  # type: ignore[attr-defined]

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"bug near {PROMPT}")

    monkeypatch.setattr(router, "handle", broken)
    r = s.client.post("/chat", json={"messages": [{"role": "user", "content": PROMPT}]})

    assert r.status_code == 500
    assert [row["event_type"] for row in s.rows] == ["request.received", "request.failed"]
    failed = s.rows[-1]
    assert failed["error_type"] == "RuntimeError"
    assert failed["actor"] == "core" and failed["outcome"] == "error"
    assert failed["route_reason"] == UNEXPECTED_ERROR_REASON
    assert_no_content(json.dumps(s.rows, default=str))
    assert PROMPT not in r.text


def test_a_crash_event_never_carries_an_odd_type_name() -> None:
    weird = type("Weird-Name", (Exception,), {})
    request = ModelRequest(messages=(ModelMessage(role="user", content="hi"),))
    event = request_crashed(weird("x"), request, RouteOptions())
    assert event.error_type == "Exception"
