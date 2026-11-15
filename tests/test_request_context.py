import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.app import create_app
from core.config import Settings
from core.request_context import (
    REQUEST_ID_HEADER,
    SESSION_ID_HEADER,
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


def test_unhandled_error_is_still_raised(client: TestClient) -> None:
    # The exception must reach the server so it is logged, not swallowed.
    with pytest.raises(RuntimeError, match="secret internal detail"):
        client.get("/_test/boom")


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
