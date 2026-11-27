"""Exit 1 of Phase 2 against a live Postgres (ADR-024 §8, step 14 of #58). Opt-in.

Core is built by ``create_app`` from Settings: its own memory store and event
writer, on Core's engine, as the app role. The providers are fakes (with the real
adapters' memory limits); everything else is real: the store's full-text
read, the selection, the prompt, the events in the database.

A fact is proposed by the model that handled request A (``propose_memory``,
ADR-033, step 15), approved by the owner through ``/memory/{id}/approve``,
and recalled in request B (handled by the other), with a
``memory.context.retrieved`` event naming that provider and the record. Every
record a test creates is deleted afterwards as the owner; events are append-only
and stay (IDs and counts only). All content is invented canaries.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.app import create_app
from core.config import Settings
from core.db import create_engine
from core.identity import load_identity
from core.log import SecretCheck
from core.request_context import is_safe_id
from memory.retrieval import MemoryRetriever
from memory.store import MemoryStore, NewRecord
from router.claude import ClaudeProvider
from router.contract import ProviderUnavailable
from router.ollama import OllamaProvider
from router.routing import Router
from tests.chat_helpers import LOCAL
from tests.conftest import LIVE_DB
from tests.fake_secrets import configured_value
from tests.fakes import CHECK, FakeProvider, snapshot
from tests.test_memory_store_live import owner_sql

pytestmark = LIVE_DB

TOKEN = configured_value() + "1"
PREFIX = "canary-recalllive-8e2a"


class Core:
    """Core with two fake providers; ``local_down`` / ``free_gib`` per request."""

    def __init__(self, settings: Settings) -> None:
        identity = load_identity()
        self.local = FakeProvider(
            "ollama",
            "local",
            memory_limit=OllamaProvider("http://localhost:11434", "m").info.memory_limit,
        )
        self.cloud = FakeProvider(
            "claude", "cloud", memory_limit=ClaudeProvider(None, "m").info.memory_limit
        )
        self.free_gib = 4.0
        app = create_app(settings, identity, router=Router([], identity, secret_check=CHECK))
        # Core's own store (create_app built it from Settings), behind the router.
        retriever = MemoryRetriever(
            app.state.memory, identity.user_context_policy.include_by_default
        )
        app.state.router = Router(
            [self.local, self.cloud],
            identity,
            secret_check=CHECK,
            probe=lambda: snapshot(self.free_gib),
            memory=retriever,
        )
        self.app = app


@pytest.fixture
def core(live_app: Settings, live_owner: Settings) -> Iterator[tuple[Core, TestClient]]:
    if owner_sql(live_owner, "SELECT to_regclass('public.memory_records')::text") != [
        "memory_records"
    ]:
        pytest.fail("memory_records missing: run `uv run alembic upgrade head`")
    before = set(owner_sql(live_owner, "SELECT id::text FROM public.memory_records"))
    settings = live_app.model_copy(update={"aether_api_token": SecretStr(TOKEN)})
    c = Core(settings)
    try:
        with TestClient(
            c.app, client=LOCAL, headers={"authorization": f"Bearer {TOKEN}"}
        ) as client:
            yield c, client
    finally:
        created = set(owner_sql(live_owner, "SELECT id::text FROM public.memory_records")) - before
        if created:
            ids = ", ".join(f"'{UUID(i)}'" for i in created)
            owner_sql(live_owner, f"DELETE FROM public.memory_records WHERE id IN ({ids})")  # noqa: S608


def propose(live_app: Settings, content: str, provider: str, request_id: str) -> str:
    """A pending fact from a model during ``request_id``, written straight to the store."""

    async def go() -> str:
        engine = create_engine(live_app)
        try:
            store = MemoryStore(engine, SecretCheck.from_settings(live_app))
            new = NewRecord(
                content=content,
                category="project",
                privacy="personal",
                source="model",
                source_provider=provider,
                source_request_id=request_id,
            )
            return str((await store.create(new)).id)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def chat(client: TestClient, text: str, **body: Any) -> Any:
    return client.post("/chat", json={"messages": [{"role": "user", "content": text}], **body})


def retrieved_events(live_owner: Settings, request_id: str) -> list[dict[str, Any]]:
    assert is_safe_id(request_id)  # our own generated ID: letters, digits, - . _
    where = f"event_type = 'memory.context.retrieved' AND request_id = '{request_id}'"
    sql = f"SELECT row_to_json(e)::text FROM events e WHERE {where} ORDER BY id"  # noqa: S608
    return [json.loads(r) for r in owner_sql(live_owner, sql)]


def recall(
    core: tuple[Core, TestClient],
    live_app: Settings,
    live_owner: Settings,
    *,
    save_via: str,
) -> None:
    c, client = core
    fact = f"{PREFIX} the owner's tea is genmaicha via {save_via}"
    if save_via == "claude":
        c.free_gib = 0.1  # no local headroom: the cloud answers
    saver = c.cloud if save_via == "claude" else c.local
    proposal = {"content": fact, "category": "preference", "privacy": "personal"}
    saver.replies = ["noted", json.dumps({"proposals": [proposal]})]
    a = chat(client, f"remember: {fact}", allow_cloud=True, propose_memory=True)
    assert a.status_code == 200 and a.json()["provider"] == save_via, a.text
    assert a.json()["memory_proposals"] == 1
    pending = client.get("/memory", params={"status": "pending"}).json()["records"]
    mine = [r for r in pending if r["content"] == fact]
    assert len(mine) == 1
    record = mine[0]
    assert (record["source"], record["source_provider"], record["privacy_source"]) == (
        "model",
        save_via,
        "suggested",
    )
    assert record["source_request_id"] == a.json()["request_id"]
    record_id = record["id"]
    approved = client.post(f"/memory/{record_id}/approve", json={"privacy": "personal"})
    assert approved.status_code == 200, approved.text

    other = "ollama" if save_via == "claude" else "claude"
    c.free_gib = 4.0
    c.local.error = ProviderUnavailable("ollama", "down") if other == "claude" else None
    b = chat(client, "which tea do I like?", allow_cloud=True)

    assert b.status_code == 200 and b.json()["provider"] == other, b.text
    receiver = c.cloud if other == "claude" else c.local
    system = receiver.requests[-1].system or ""
    assert fact in system.split("Relevant memory:", 1)[1]
    rows = [
        e for e in retrieved_events(live_owner, b.json()["request_id"]) if e["provider"] == other
    ]
    assert len(rows) == 1
    assert record_id in rows[0]["memory_record_ids"]
    assert rows[0]["memory_effective_level"] == "personal"
    # Events carry IDs and counts only.
    assert PREFIX not in json.dumps(retrieved_events(live_owner, b.json()["request_id"]))


def test_live_saved_via_local_recalled_via_cloud(
    core: tuple[Core, TestClient], live_app: Settings, live_owner: Settings
) -> None:
    recall(core, live_app, live_owner, save_via="ollama")


def test_live_saved_via_cloud_recalled_locally(
    core: tuple[Core, TestClient], live_app: Settings, live_owner: Settings
) -> None:
    recall(core, live_app, live_owner, save_via="claude")


def test_live_pending_fact_is_not_recalled(
    core: tuple[Core, TestClient], live_app: Settings, live_owner: Settings
) -> None:
    c, client = core
    fact = f"{PREFIX} pending genmaicha"
    propose(live_app, fact, "ollama", "req-recalllive-pending")

    b = chat(client, "which tea do I like? genmaicha")

    assert b.status_code == 200
    assert fact not in (c.local.requests[-1].system or "")


def test_live_proposals_are_pending_secret_shaped_ones_dropped(
    core: tuple[Core, TestClient], live_owner: Settings
) -> None:
    from tests.chat_helpers import FAKE_KEY

    c, client = core
    fact = f"{PREFIX} the owner grows basil"
    items = [
        {"content": fact, "category": "project", "privacy": "public"},
        {"content": f"my key is {FAKE_KEY}", "category": "reference", "privacy": "personal"},
    ]
    c.local.replies = ["ok", json.dumps({"proposals": items})]

    a = chat(client, f"{PREFIX} I grow basil", propose_memory=True)

    assert a.status_code == 200 and a.json()["memory_proposals"] == 1
    rid = a.json()["request_id"]
    assert is_safe_id(rid)
    records = [
        json.loads(r)
        for r in owner_sql(
            live_owner,
            "SELECT row_to_json(m)::text FROM public.memory_records m"  # noqa: S608
            f" WHERE source_request_id = '{rid}'",
        )
    ]
    assert [(r["content"], r["status"], r["privacy"], r["source"]) for r in records] == [
        (fact, "pending", "personal", "model")  # floored to the request's level
    ]
    events = [
        json.loads(r)
        for r in owner_sql(
            live_owner,
            "SELECT row_to_json(e)::text FROM events e WHERE event_type = 'memory.proposed'"  # noqa: S608
            f" AND memory_record_id = '{UUID(records[0]['id'])}'",
        )
    ]
    assert len(events) == 1 and events[0]["memory_source"] == "model"
    assert PREFIX not in json.dumps(events) and FAKE_KEY not in json.dumps(events)
