import asyncio
import io
import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.types import Message, Receive, Scope, Send

from core.app import create_app
from core.config import Settings
from core.log import configure_logging
from core.request_context import (
    REQUEST_ID_HEADER,
    SESSION_ID_HEADER,
    RequestContextMiddleware,
    UnhandledRequestError,
    get_request_id,
    get_session_id,
    is_safe_id,
)

TEST_TOKEN = "fake-api-token-fixture-0123456789abcdef"  # noqa: S105  # gitleaks:allow
AUTH = {"authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def app() -> FastAPI:
    app = create_app(Settings(_env_file=None, aether_api_token=SecretStr(TEST_TOKEN)))

    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/_test/context")
    async def context() -> dict[str, str | None]:
        return {"request_id": get_request_id(), "session_id": get_session_id()}

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers=AUTH)


def test_generates_uuid_request_id_when_absent(client: TestClient) -> None:
    response = client.get("/health")

    request_id = response.headers[REQUEST_ID_HEADER]
    assert uuid.UUID(request_id).version == 4


def test_each_request_gets_a_new_id(client: TestClient) -> None:
    first = client.get("/health").headers[REQUEST_ID_HEADER]
    second = client.get("/health").headers[REQUEST_ID_HEADER]

    assert first != second


def test_valid_incoming_request_id_is_kept(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "n8n-run_42.a"})

    assert response.headers[REQUEST_ID_HEADER] == "n8n-run_42.a"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "has space",
        "semi;colon",
        "a" * 129,
        "<script>",
        "id\tTAB",
        "naïve".encode("latin-1"),  # non-ASCII bytes, as a raw client could send
    ],
)
def test_unsafe_incoming_request_id_is_replaced(client: TestClient, bad_id: str | bytes) -> None:
    raw = bad_id if isinstance(bad_id, bytes) else bad_id.encode("latin-1")
    response = client.get("/_test/context", headers={REQUEST_ID_HEADER.encode(): raw})

    returned = response.headers[REQUEST_ID_HEADER]
    assert returned != raw.decode("latin-1")
    assert uuid.UUID(returned).version == 4
    assert response.json()["request_id"] == returned


def test_ids_are_available_inside_the_endpoint(client: TestClient) -> None:
    response = client.get(
        "/_test/context",
        headers={REQUEST_ID_HEADER: "req-1", SESSION_ID_HEADER: "sess-1"},
    )

    assert response.json() == {"request_id": "req-1", "session_id": "sess-1"}
    assert response.headers[SESSION_ID_HEADER] == "sess-1"


def test_session_id_absent_by_default(client: TestClient) -> None:
    response = client.get("/_test/context")

    assert response.json()["session_id"] is None
    assert SESSION_ID_HEADER not in response.headers


def test_unsafe_session_id_is_dropped(client: TestClient) -> None:
    response = client.get("/_test/context", headers={SESSION_ID_HEADER: "bad session"})

    assert response.json()["session_id"] is None
    assert SESSION_ID_HEADER not in response.headers


def test_request_id_present_on_404(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert REQUEST_ID_HEADER in response.headers


def test_request_id_present_on_unhandled_error(app: FastAPI) -> None:
    client = TestClient(app, headers=AUTH, raise_server_exceptions=False)

    response = client.get(
        "/_test/boom", headers={REQUEST_ID_HEADER: "req-500", SESSION_ID_HEADER: "s-500"}
    )

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "req-500"
    assert response.headers[SESSION_ID_HEADER] == "s-500"
    assert response.json() == {"detail": "Internal Server Error"}
    assert "secret internal detail" not in response.text


def test_unhandled_error_still_reaches_the_server_without_its_message(
    client: TestClient,
) -> None:
    # The error must reach the server so it is logged, not swallowed; but as a
    # stand-in with the type only, so the server's log has no message (#124).
    with pytest.raises(UnhandledRequestError) as raised:
        client.get("/_test/boom")
    assert raised.value.error_type == "RuntimeError"
    assert "secret internal detail" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_an_error_after_the_response_started_sends_no_second_response() -> None:
    # Once the status line is out, a 500 can't be sent: the middleware must send
    # nothing more and let the error reach the server.
    sent: list[Message] = []

    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("midstream failure")

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "path": "/x", "headers": []}
    with pytest.raises(UnhandledRequestError, match="RuntimeError"):
        asyncio.run(RequestContextMiddleware(failing_app)(scope, receive, send))

    assert [m["type"] for m in sent] == ["http.response.start"]
    assert sent[0]["status"] == 200
    assert get_request_id() is None  # the context is cleared even so


def test_context_is_cleared_after_request(client: TestClient) -> None:
    client.get("/_test/context", headers={REQUEST_ID_HEADER: "req-1", SESSION_ID_HEADER: "s-1"})

    assert get_request_id() is None
    assert get_session_id() is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("0b9e4c1e-7c55-4f8e-9d0a-1a2b3c4d5e6f", True),
        ("01HZY8K3Q7RZ6X0V8M2N4P6S9T", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("line\nbreak", False),
    ],
)
def test_is_safe_id(value: str | None, expected: bool) -> None:
    assert is_safe_id(value) is expected


def test_an_id_holding_a_configured_secret_is_replaced(client: TestClient) -> None:
    # The token is a safe ID by its characters; Core still never adopts it (#75), so
    # it is not echoed, logged, put in an event or stored with a memory record.
    response = client.get(
        "/_test/context",
        headers={REQUEST_ID_HEADER: TEST_TOKEN, SESSION_ID_HEADER: f"s-{TEST_TOKEN}"},
    )

    assert response.headers[REQUEST_ID_HEADER] != TEST_TOKEN
    assert SESSION_ID_HEADER not in response.headers
    assert response.json() == {
        "request_id": response.headers[REQUEST_ID_HEADER],
        "session_id": None,
    }
    assert TEST_TOKEN not in response.text


def test_a_failing_500_send_still_raises_the_stand_in() -> None:
    # The client went away while the 500 was sent: the send error must not escape,
    # or its traceback would chain the original message back in (#124).
    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("detail CANARY-SEND-0042")

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        raise OSError("client disconnected")

    scope: Scope = {"type": "http", "path": "/x", "headers": []}
    with pytest.raises(UnhandledRequestError) as raised:
        asyncio.run(RequestContextMiddleware(failing_app)(scope, receive, send))
    assert raised.value.__suppress_context__


def test_the_servers_own_log_of_the_stand_in_has_no_message(
    client: TestClient,
) -> None:
    # Log the stand-in the way uvicorn does, through AETHER's JSON handler.
    out = io.StringIO()
    configure_logging(Settings(_env_file=None), stream=out)
    with pytest.raises(UnhandledRequestError) as raised:
        client.get("/_test/boom")
    logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=raised.value)
    # The server's record only (the middleware's own record has the frames, whose
    # source lines are code, here including this test app's literal).
    [text] = [line for line in out.getvalue().splitlines() if '"uvicorn.error"' in line]
    assert "UnhandledRequestError" in text
    assert "secret internal detail" not in text
    assert "During handling" not in text and "direct cause" not in text
