"""The memory API without a database (#92, ADR-024 §2 and §4; #75; #80).

Core is built by the real ``create_app`` wiring. ``DATABASE_URL`` points at a port
where nothing listens (the encrypted volume locked, Postgres not running), so:

- every refusal checked here happens before the database (a 422, not a 503);
- every request that would reach the database answers 503, never an empty list,
  a 404 or a silent write (#80).

Success paths against a live Postgres are in tests/test_memory_api_live.py. All
content, keys and tokens are invented canaries; responses and logs are checked
not to contain them.
"""

import io
import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.app import create_app
from core.config import Settings
from core.identity import load_identity
from core.log import configure_logging
from core.memory_api import MAX_BODY_BYTES, UNAVAILABLE_MESSAGE
from core.request_context import REQUEST_ID_HEADER
from memory.store import QUERY_MAX, MemoryStore
from router.routing import Router
from tests.chat_helpers import LOCAL
from tests.fake_secrets import by_family, configured_value
from tests.fakes import CHECK, FakeProvider, snapshot

# Lowercase hex after a lowercase prefix: valid in a key and in a request ID, so the
# configured-value check is the only thing that can refuse it there. 32 characters,
# the minimum API token length.
TOKEN = configured_value() + "0"
AUTH = {"authorization": f"Bearer {TOKEN}"}
DB_PASSWORD = "fake-db-pw-memapi-" + configured_value()
DOWN_URL = f"postgresql+asyncpg://aether_app:{DB_PASSWORD}@127.0.0.1:1/aether"
CANARY = "canary-memapi-8b17 the owner's invented favourite colour is teal"
CANARY_KEY = "canary.memapi_8b17"  # gitleaks:allow (invented memory key, not a secret)


class Core:
    """Core as uvicorn builds it, with a fake local provider and logs captured."""

    def __init__(self, database_url: str | None = DOWN_URL) -> None:
        self.settings = Settings(
            _env_file=None,
            aether_api_token=SecretStr(TOKEN),
            database_url=SecretStr(database_url) if database_url else None,
        )
        identity = load_identity()
        router = Router(
            [FakeProvider("ollama", "local")],
            identity,
            secret_check=CHECK,
            probe=lambda: snapshot(4.0),
        )
        self.app = create_app(self.settings, identity, router=router)
        self.log = io.StringIO()
        configure_logging(self.settings, stream=self.log)

    def everything(self, response: Any) -> str:
        """The response (headers and body) and every log line so far."""
        return f"{dict(response.headers)}\n{response.text}\n{self.log.getvalue()}"


@pytest.fixture
def core() -> Iterator[tuple[Core, TestClient]]:
    c = Core()
    # As a context manager: runs startup (database check) and shutdown (dispose).
    with TestClient(c.app, client=LOCAL, headers=AUTH, raise_server_exceptions=False) as client:
        yield c, client


def record(**changes: Any) -> dict[str, Any]:
    return {"content": CANARY, "category": "preference", "privacy": "personal", **changes}


def field_errors(response: Any) -> list[list[str]]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["type"] == "ValidationError"
    return [f["loc"] for f in body["error"]["fields"]]


def assert_clean(c: Core, response: Any, *values: str) -> None:
    text = c.everything(response)
    for value in (CANARY, CANARY_KEY, TOKEN, DB_PASSWORD, *values):
        assert value not in text


# --- #75: one store, built from Settings with the configured values ------------------


def test_core_builds_one_store_from_settings_with_configured_values() -> None:
    c = Core()
    store = c.app.state.memory
    assert isinstance(store, MemoryStore)
    # The same check the store uses: it knows the configured API token.
    assert store._looks_secret(f"a note with {TOKEN} inside") is True
    assert store._looks_secret("a note about tea") is False


def test_a_configured_value_in_content_is_refused_end_to_end(
    core: tuple[Core, TestClient],
) -> None:
    c, client = core
    r = client.post("/memory", json=record(content=f"{CANARY} {TOKEN}"))
    assert field_errors(r) == [["body", "content"]]
    assert "looks like a secret" in r.json()["error"]["fields"][0]["msg"]
    assert_clean(c, r)


def test_a_configured_value_in_the_key_is_refused_end_to_end(
    core: tuple[Core, TestClient],
) -> None:
    c, client = core
    key = f"note.{TOKEN}"
    r = client.post("/memory", json=record(key=key))
    assert field_errors(r) == [["body", "key"]]
    assert_clean(c, r, key)


@pytest.mark.parametrize("fake", by_family(TOKEN), ids=lambda f: f.family)
def test_secret_shaped_content_is_refused(core: tuple[Core, TestClient], fake: Any) -> None:
    c, client = core
    r = client.post("/memory", json=record(content=fake.text))
    assert field_errors(r) == [["body", "content"]]
    assert_clean(c, r, fake.core)


def test_a_configured_value_as_request_id_is_never_used(core: tuple[Core, TestClient]) -> None:
    # source_request_id is Core's request ID. A client-sent ID holding a configured
    # value is replaced by a new one, so it is never echoed, logged or stored.
    c, client = core
    r = client.post("/memory", json=record(), headers={REQUEST_ID_HEADER: TOKEN})
    assert r.status_code == 503  # got past every check; the database is down
    assert r.headers[REQUEST_ID_HEADER] != TOKEN
    assert r.json()["request_id"] == r.headers[REQUEST_ID_HEADER]
    assert_clean(c, r)


def test_an_ordinary_request_id_is_kept(core: tuple[Core, TestClient]) -> None:
    _, client = core
    r = client.get("/memory", headers={REQUEST_ID_HEADER: "req-memapi.1"})
    assert r.headers[REQUEST_ID_HEADER] == "req-memapi.1"


# --- POST /memory: level required, Secret refused, the body can't claim authority ----


def test_privacy_is_required_and_never_defaulted(core: tuple[Core, TestClient]) -> None:
    c, client = core
    body = record()
    del body["privacy"]
    r = client.post("/memory", json=body)
    assert field_errors(r) == [["body", "privacy"]]
    assert_clean(c, r)


def test_secret_level_is_refused(core: tuple[Core, TestClient]) -> None:
    c, client = core
    r = client.post("/memory", json=record(privacy="secret"))
    assert field_errors(r) == [["body", "privacy"]]
    assert "never stored" in r.json()["error"]["fields"][0]["msg"]
    assert_clean(c, r)


@pytest.mark.parametrize(
    "extra",
    [
        {"source": "owner"},
        {"source": "model"},
        {"status": "approved"},
        {"privacy_source": "owner"},
        {"source_request_id": "req-1"},
        {"source_provider": "ollama"},
        {"id": str(uuid4())},
    ],
)
def test_the_body_cannot_set_who_or_what_state(
    core: tuple[Core, TestClient], extra: dict[str, str]
) -> None:
    c, client = core
    r = client.post("/memory", json=record(**extra))
    assert field_errors(r) == [["body", next(iter(extra))]]
    assert_clean(c, r)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"content": ""}, "content"),
        ({"content": CANARY * 20}, "content"),  # over 1000 characters
        ({"content": f"{CANARY}\x00"}, "content"),
        ({"key": "Not A Key " + CANARY}, "key"),
        ({"category": "diary"}, "category"),
        ({"privacy": "top-secret"}, "privacy"),
        ({"expires_at": "2030-01-01T00:00:00"}, "expires_at"),  # no time zone
    ],
)
def test_invalid_records_are_refused_without_echo(
    core: tuple[Core, TestClient], changes: dict[str, str], field: str
) -> None:
    c, client = core
    r = client.post("/memory", json=record(**changes))
    assert field_errors(r) == [["body", field]]
    assert_clean(c, r)


def test_body_size_is_capped(core: tuple[Core, TestClient]) -> None:
    _, client = core
    big = json.dumps(record(content="x" * (MAX_BODY_BYTES + 1)))
    for path in ("/memory", "/memory/search"):
        r = client.post(path, content=big, headers={"content-type": "application/json"})
        assert r.status_code == 413
        assert r.json()["error"]["type"] == "RequestTooLarge"


def test_memory_routes_need_the_token() -> None:
    c = Core()
    client = TestClient(c.app, client=LOCAL)
    calls = [
        ("GET", "/memory", None),
        ("GET", f"/memory/{uuid4()}", None),
        ("POST", "/memory/search", {"query": "teal"}),
        ("POST", "/memory", record()),
    ]
    for method, path, body in calls:
        r = client.request(method, path, json=body)
        assert r.status_code == 401, path


# --- GET /memory, GET /memory/{id}, POST /memory/search: input checks ----------------


def test_list_takes_only_fixed_filters_in_the_url(core: tuple[Core, TestClient]) -> None:
    # A key or a text query is content: never in a URL, which the access log records.
    _, client = core
    for name, value in (("key", CANARY_KEY), ("query", CANARY)):
        r = client.get("/memory", params={name: value})
        assert field_errors(r) == [["query", name]]
        assert CANARY not in r.text and CANARY_KEY not in r.text


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"status": "rejected"}, "status"),
        ({"category": "diary"}, "category"),
        ({"privacy": "secret"}, "privacy"),  # no secret records exist to list
        ({"limit": "0"}, "limit"),
        ({"limit": "201"}, "limit"),
        ({"offset": "-1"}, "offset"),
    ],
)
def test_list_filters_are_checked(
    core: tuple[Core, TestClient], params: dict[str, str], field: str
) -> None:
    _, client = core
    assert field_errors(client.get("/memory", params=params)) == [["query", field]]


def test_get_needs_a_uuid(core: tuple[Core, TestClient]) -> None:
    _, client = core
    assert field_errors(client.get("/memory/not-a-uuid")) == [["path", "record_id"]]


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({}, ["body"]),  # neither key nor query
        ({"query": ""}, ["body", "query"]),
        ({"query": "x" * (QUERY_MAX + 1)}, ["body", "query"]),
        ({"query": "teal\x00"}, ["body", "query"]),
        ({"key": "Not A Key"}, ["body", "key"]),
        ({"query": "teal", "privacy": "secret"}, ["body", "privacy"]),
        ({"query": "teal", "limit": 0}, ["body", "limit"]),
        ({"query": "teal", "source": "model"}, ["body", "source"]),
    ],
)
def test_search_input_is_checked(
    core: tuple[Core, TestClient], body: dict[str, Any], loc: list[str]
) -> None:
    _, client = core
    assert field_errors(client.post("/memory/search", json=body)) == [loc]


# --- #80: no database means 503, never an empty result -------------------------------


def _every_route(client: TestClient) -> list[Any]:
    return [
        client.get("/memory"),
        client.get("/memory", params={"status": "pending"}),
        client.get(f"/memory/{uuid4()}"),
        client.post("/memory/search", json={"key": CANARY_KEY, "query": CANARY}),
        client.post("/memory", json=record(key=CANARY_KEY)),
    ]


def _assert_unavailable(c: Core, responses: list[Any]) -> None:
    for r in responses:
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["error"] == {
            "type": "MemoryUnavailable",
            "message": UNAVAILABLE_MESSAGE,
            "attempts": [],
        }
        assert "records" not in body
        assert_clean(c, r)


def test_unreachable_database_is_503_on_every_route(core: tuple[Core, TestClient]) -> None:
    c, client = core
    responses = _every_route(client)
    _assert_unavailable(c, responses)
    warnings = [
        json.loads(line) for line in c.log.getvalue().splitlines() if "database unavailable" in line
    ]
    assert {w["operation"] for w in warnings} == {"list", "get", "search", "create"}
    assert all(w["level"] == "WARNING" for w in warnings)


def test_no_database_configured_is_503_on_every_route() -> None:
    c = Core(database_url=None)
    assert c.app.state.memory is None
    with TestClient(c.app, client=LOCAL, headers=AUTH, raise_server_exceptions=False) as client:
        _assert_unavailable(c, _every_route(client))
