"""The memory API against a live Postgres, as the app role (#92). Opt-in.

Core is built by ``create_app`` from Settings, so the store is the one Core builds
(``SecretCheck.from_settings``, #75), on Core's engine with the privilege check.
Every record a test creates is deleted afterwards as the owner. All content and
keys are invented canaries.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.app import create_app
from core.config import Settings
from core.identity import load_identity
from core.request_context import REQUEST_ID_HEADER
from router.routing import Router
from tests.chat_helpers import LOCAL
from tests.conftest import LIVE_DB
from tests.fake_secrets import configured_value
from tests.fakes import CHECK, FakeProvider, snapshot
from tests.test_memory_store_live import events_after, last_event_id, owner_sql

pytestmark = LIVE_DB

TOKEN = configured_value() + "0"
PREFIX = "canary-memapilive-c4e9"
KEY = "canary.memapilive_c4e9"  # gitleaks:allow (invented memory key, not a secret)


def content(n: int | str) -> str:
    return f"{PREFIX} invented fact {n}"


def record(n: int | str = 1, **changes: Any) -> dict[str, Any]:
    return {"content": content(n), "category": "preference", "privacy": "personal", **changes}


@pytest.fixture
def client(live_app: Settings, live_owner: Settings) -> Iterator[TestClient]:
    tables = owner_sql(live_owner, "SELECT to_regclass('public.memory_records')::text")
    if tables != ["memory_records"]:
        pytest.fail("memory_records missing: run `uv run alembic upgrade head`")
    before = set(owner_sql(live_owner, "SELECT id::text FROM public.memory_records"))
    settings = live_app.model_copy(update={"aether_api_token": SecretStr(TOKEN)})
    identity = load_identity()
    router = Router(
        [FakeProvider("ollama", "local")], identity, secret_check=CHECK, probe=lambda: snapshot(4.0)
    )
    app = create_app(settings, identity, router=router)
    headers = {"authorization": f"Bearer {TOKEN}"}
    try:
        with TestClient(app, client=LOCAL, headers=headers) as test_client:
            yield test_client
    finally:
        after = set(owner_sql(live_owner, "SELECT id::text FROM public.memory_records"))
        created = after - before
        if created:
            ids = ", ".join(f"'{UUID(i)}'" for i in created)
            owner_sql(live_owner, f"DELETE FROM public.memory_records WHERE id IN ({ids})")  # noqa: S608


def test_owner_creates_an_approved_record(client: TestClient, live_owner: Settings) -> None:
    after = last_event_id(live_owner)
    r = client.post(
        "/memory", json=record(key=KEY, privacy="sensitive"), headers={REQUEST_ID_HEADER: "req-1"}
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content"] == content(1) and body["key"] == KEY
    assert (body["status"], body["source"], body["privacy_source"]) == (
        "approved",
        "owner",
        "owner",
    )
    assert body["privacy"] == "sensitive"
    assert body["source_request_id"] == "req-1"  # Core's request ID, not the body's
    assert body["source_provider"] is None and body["approved_at"] is not None
    events = events_after(live_owner, after)
    assert [(e["event_type"], e["memory_record_id"]) for e in events] == [
        ("memory.approved", body["id"])
    ]


def test_get_returns_the_record_and_404_for_an_unknown_id(client: TestClient) -> None:
    created = client.post("/memory", json=record()).json()

    r = client.get(f"/memory/{created['id']}")
    assert r.status_code == 200 and r.json() == created

    missing = client.get(f"/memory/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"] == {
        "type": "NotFound",
        "message": "memory record not found",
        "attempts": [],
    }


def test_list_filters_and_pages(client: TestClient) -> None:
    ids = [
        client.post("/memory", json=record(n, privacy=level, category=category)).json()["id"]
        for n, level, category in (
            (1, "public", "preference"),
            (2, "personal", "project"),
            (3, "personal", "preference"),
        )
    ]

    def found(**params: Any) -> list[str]:
        r = client.get("/memory", params={"limit": 200, **params})
        assert r.status_code == 200, r.text
        return [x["id"] for x in r.json()["records"] if x["id"] in ids]

    assert found() == ids[::-1]  # newest first
    assert found(privacy="personal") == [ids[2], ids[1]]
    assert found(category="preference", privacy="personal") == [ids[2]]
    assert found(status="pending") == []  # owner records are approved at once

    page = client.get("/memory", params={"limit": 1, "offset": 0}).json()
    assert (page["limit"], page["offset"], len(page["records"])) == (1, 0, 1)


def test_search_by_key_and_by_text(client: TestClient) -> None:
    keyed = client.post("/memory", json=record("Tea", key=KEY)).json()
    other = client.post("/memory", json=record("coffee 100% arabica")).json()

    def search(**body: Any) -> list[str]:
        r = client.post("/memory/search", json={"limit": 200, **body})
        assert r.status_code == 200, r.text
        return [x["id"] for x in r.json()["records"]]

    assert search(key=KEY) == [keyed["id"]]
    assert search(query=f"{PREFIX} INVENTED fact tea") == [keyed["id"]]  # any case
    assert search(query="100% arabica") == [other["id"]]
    assert search(query="0_% arabica") == []  # '%' and '_' are plain characters
    assert search(query=PREFIX, privacy="public") == []
    assert search(key=KEY, query="coffee") == []  # both must match


def test_a_key_used_by_another_approved_record_is_409(client: TestClient) -> None:
    assert client.post("/memory", json=record(1, key=KEY)).status_code == 201

    r = client.post("/memory", json=record(2, key=KEY))

    assert r.status_code == 409
    assert r.json()["error"]["type"] == "KeyConflict"
    assert KEY not in r.text and content(2) not in r.text


def test_refused_records_write_nothing(client: TestClient, live_owner: Settings) -> None:
    after = last_event_id(live_owner)
    count = owner_sql(live_owner, "SELECT count(*) FROM public.memory_records")

    for body in (
        record(privacy="secret"),
        record(content=f"{content(1)} {TOKEN}"),
        {k: v for k, v in record().items() if k != "privacy"},
    ):
        assert client.post("/memory", json=body).status_code == 422

    assert owner_sql(live_owner, "SELECT count(*) FROM public.memory_records") == count
    assert events_after(live_owner, after) == []
