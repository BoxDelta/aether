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
from core.errors import UNKNOWN_FIELD
from core.identity import load_identity
from core.log import configure_logging
from core.memory_api import MAX_BODY_BYTES, OFFSET_MAX, UNAVAILABLE_MESSAGE
from core.request_context import REQUEST_ID_HEADER
from memory.store import QUERY_MAX, MemoryStore, StoreFailed
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
    for method, path in (
        ("POST", "/memory"),
        ("POST", "/memory/search"),
        ("POST", f"/memory/{uuid4()}/approve"),
        ("PATCH", f"/memory/{uuid4()}"),
    ):
        r = client.request(method, path, content=big, headers={"content-type": "application/json"})
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
        ("POST", f"/memory/{uuid4()}/approve", {"privacy": "public"}),
        ("POST", f"/memory/{uuid4()}/reject", None),
        ("PATCH", f"/memory/{uuid4()}", {"content": CANARY}),
        ("DELETE", f"/memory/{uuid4()}", None),
        ("GET", "/memory/export", None),
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
    ("name", "value"),
    [("privacy", "public"), ("status", "approved"), ("key", CANARY_KEY), ("query", CANARY)],
)
def test_export_takes_no_filters(core: tuple[Core, TestClient], name: str, value: str) -> None:
    # The export is always everything (D10): a filter must never look like it worked.
    c, client = core
    r = client.get("/memory/export", params={name: value})
    assert field_errors(r) == [["query", name]]
    assert_clean(c, r)
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
        ({"offset": str(OFFSET_MAX + 1)}, "offset"),
        ({"offset": "9223372036854775808"}, "offset"),  # one past bigint: was a 500
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
        ({"query": "teal", "offset": OFFSET_MAX + 1}, ["body", "offset"]),
        ({"query": "teal", "offset": 2**63}, ["body", "offset"]),  # was a 500
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
        client.post(f"/memory/{uuid4()}/approve", json={"privacy": "public", "key": CANARY_KEY}),
        client.post(f"/memory/{uuid4()}/reject"),
        client.patch(f"/memory/{uuid4()}", json={"content": CANARY, "key": CANARY_KEY}),
        client.delete(f"/memory/{uuid4()}"),
        client.get("/memory/export"),
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


class _BrokenStore:
    """A store whose every read and write fails with a database error that is not
    "unavailable", to reach the API's 500 branch."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def list(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def get(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def create(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def approve(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def reject(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def update(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def delete(self, *_: Any, **__: Any) -> Any:
        raise self.error

    async def export(self, *_: Any, **__: Any) -> Any:
        raise self.error


def test_an_unexpected_store_error_is_a_fixed_500_without_details(
    core: tuple[Core, TestClient],
) -> None:
    c, client = core
    c.app.state.memory = _BrokenStore(StoreFailed("database error (XX000)"))

    responses = [
        client.get("/memory"),
        client.get(f"/memory/{uuid4()}"),
        client.post("/memory", json=record(key=CANARY_KEY)),
        client.post(f"/memory/{uuid4()}/approve", json={"privacy": "public"}),
        client.post(f"/memory/{uuid4()}/reject"),
        client.patch(f"/memory/{uuid4()}", json={"content": CANARY}),
        client.delete(f"/memory/{uuid4()}"),
        client.get("/memory/export"),
    ]

    for r in responses:
        assert r.status_code == 500, r.text
        assert r.json()["error"] == {
            "type": "MemoryStoreFailed",
            "message": "the memory store failed",
            "attempts": [],
        }
        assert_clean(c, r)
    failures = [
        json.loads(line)
        for line in c.log.getvalue().splitlines()
        if "Memory " in line and " failed" in line
    ]
    operations = {"list", "get", "create", "approve", "reject", "update", "delete", "export"}
    assert {f["operation"] for f in failures} == operations
    for failure in failures:
        assert failure["level"] == "ERROR"
        assert failure["error"] == "database error (XX000)"  # type and SQLSTATE only


def test_unreachable_database_is_503_on_every_route(core: tuple[Core, TestClient]) -> None:
    c, client = core
    responses = _every_route(client)
    _assert_unavailable(c, responses)
    warnings = [
        json.loads(line) for line in c.log.getvalue().splitlines() if "database unavailable" in line
    ]
    assert {w["operation"] for w in warnings} == {
        "list",
        "get",
        "search",
        "create",
        "approve",
        "reject",
        "update",
        "delete",
        "export",
    }
    assert all(w["level"] == "WARNING" for w in warnings)


def test_no_database_configured_is_503_on_every_route() -> None:
    c = Core(database_url=None)
    assert c.app.state.memory is None
    with TestClient(c.app, client=LOCAL, headers=AUTH, raise_server_exceptions=False) as client:
        _assert_unavailable(c, _every_route(client))


# --- approve and reject (step 8, #97): input checks before the database ----------------


def _approve(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(f"/memory/{uuid4()}/approve", json=body)


def test_approve_needs_the_owners_level(core: tuple[Core, TestClient]) -> None:
    c, client = core
    r = _approve(client, {"content": CANARY})
    assert field_errors(r) == [["body", "privacy"]]
    assert_clean(c, r)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"privacy": "secret"}, "privacy"),
        ({"privacy": "public", "content": f"{CANARY} {TOKEN}"}, "content"),
        ({"privacy": "public", "key": f"note.{TOKEN}"}, "key"),
        ({"privacy": "public", "content": ""}, "content"),
        ({"privacy": "public", "key": "Not A Key"}, "key"),
        ({"privacy": "public", "category": "diary"}, "category"),
        ({"privacy": "public", "status": "approved"}, "status"),
        ({"privacy": "public", "source": "owner"}, "source"),
        ({"privacy": "public", "expires_at": "2030-01-01T00:00:00+00:00"}, "expires_at"),
    ],
)
def test_approve_input_is_checked_without_echo(
    core: tuple[Core, TestClient], body: dict[str, Any], field: str
) -> None:
    c, client = core
    r = _approve(client, body)
    assert field_errors(r) == [["body", field]]
    assert_clean(c, r, f"note.{TOKEN}")


def test_approve_and_reject_need_a_uuid(core: tuple[Core, TestClient]) -> None:
    _, client = core
    r = client.post("/memory/not-a-uuid/approve", json={"privacy": "public"})
    assert field_errors(r) == [["path", "record_id"]]
    assert field_errors(client.post("/memory/not-a-uuid/reject")) == [["path", "record_id"]]


def test_approve_refuses_null_for_what_it_cannot_clear(core: tuple[Core, TestClient]) -> None:
    # null is never "unchanged": only the key can be cleared.
    _, client = core
    for name in ("content", "category"):
        r = _approve(client, {"privacy": "public", name: None})
        assert field_errors(r) == [["body"]], name


# --- PATCH and DELETE (step 9, #101): input checks before the database -----------------


def _patch(client: TestClient, body: Any) -> Any:
    return client.patch(f"/memory/{uuid4()}", json=body)


@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({}, ["body", "changes"]),  # nothing to change
        ({"privacy": "secret"}, ["body", "privacy"]),
        ({"content": f"{CANARY} {TOKEN}"}, ["body", "content"]),
        ({"key": f"note.{TOKEN}"}, ["body", "key"]),
        ({"content": ""}, ["body", "content"]),
        ({"content": None}, ["body"]),
        ({"category": None}, ["body"]),
        ({"privacy": None}, ["body"]),
        ({"key": "Not A Key"}, ["body", "key"]),
        ({"expires_at": "2030-01-01T00:00:00"}, ["body", "expires_at"]),
        ({"status": "approved"}, ["body", "status"]),
        ({"source": "owner"}, ["body", "source"]),
        ({"privacy_source": "owner"}, ["body", "privacy_source"]),
    ],
)
def test_patch_input_is_checked_without_echo(
    core: tuple[Core, TestClient], body: dict[str, Any], loc: list[str]
) -> None:
    c, client = core
    r = _patch(client, body)
    assert field_errors(r) == [loc]
    assert_clean(c, r, f"note.{TOKEN}")


def test_patch_and_delete_need_a_uuid(core: tuple[Core, TestClient]) -> None:
    _, client = core
    r = client.patch("/memory/not-a-uuid", json={"content": "x"})
    assert field_errors(r) == [["path", "record_id"]]
    assert field_errors(client.delete("/memory/not-a-uuid")) == [["path", "record_id"]]


# --- an unknown field's name is the sender's text (#94) -----------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "loc"),
    [
        ("post", "/memory", {"json": record(**{"diagnosis.migraine": "x"})}, ["body"]),
        ("post", "/memory/search", {"json": {"query": "teal", "Sam's address": 1}}, ["body"]),
        ("get", "/memory", {"params": {"diagnosis.migraine": "1"}}, ["query"]),
        ("get", "/memory/export", {"params": {"my-bank-pin": "4711"}}, ["query"]),
    ],
)
def test_an_unknown_field_name_is_never_echoed(
    core: tuple[Core, TestClient],
    method: str,
    path: str,
    kwargs: dict[str, Any],
    loc: list[str],
) -> None:
    c, client = core
    r = getattr(client, method)(path, **kwargs)
    assert field_errors(r) == [[*loc, UNKNOWN_FIELD]]
    for name in ("diagnosis", "migraine", "Sam", "bank", "4711"):
        assert name not in r.text
    assert_clean(c, r)


def test_one_of_the_apis_own_names_is_still_named(core: tuple[Core, TestClient]) -> None:
    # "source" is AETHER's name, not the sender's text: naming it says what was wrong.
    _, client = core
    r = client.post("/memory", json=record(source="model"))
    assert field_errors(r) == [["body", "source"]]
