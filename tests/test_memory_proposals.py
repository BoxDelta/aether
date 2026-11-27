"""Model memory proposals (ADR-033; step 15 of #58): parsing, the level floor,
storing, ``Router.call_once`` and ``/chat`` with ``propose_memory``. No database:
a fake store. The live store path is in tests/test_memory_proposals_live.py.
"""

import asyncio
import json
from typing import Any

import pytest

from core.identity import MEMORY_HEADING, Identity, load_identity
from memory.proposals import (
    INSTRUCTIONS,
    MAX_PROPOSALS,
    PROPOSAL_MAX_TOKENS,
    Parsed,
    Proposal,
    floored,
    parse,
    proposal_request,
    store_proposals,
)
from memory.store import NewRecord, PendingLimit, SecretContent, StoreUnavailable
from router.contract import Message, ModelRequest, ProviderUnavailable
from router.routing import NoRoute, RouteOptions, Router
from tests.chat_helpers import PROMPT, Setup
from tests.fakes import CHECK, FakeProvider, snapshot
from tests.test_memory_chat import FakeSource, record

CANARY = "canary-proposal-7c3e"


def reply(*items: dict[str, Any]) -> str:
    return json.dumps({"proposals": list(items)})


def item(content: Any = f"{CANARY} likes tea", **kw: Any) -> dict[str, Any]:
    return {"content": content, "category": "preference", "privacy": "personal", **kw}


class FakeStore:
    """Records what it is asked to create; ``fail`` maps a call number (0-based) to
    the error that call raises."""

    def __init__(self, fail: dict[int, Exception] | None = None) -> None:
        self.created: list[NewRecord] = []
        self.fail = fail or {}
        self.calls = 0

    async def create(self, new: NewRecord) -> object:
        call = self.calls
        self.calls += 1
        if call in self.fail:
            raise self.fail[call]
        self.created.append(new)
        return object()


# --- parse ---------------------------------------------------------------------------


def test_parse_takes_valid_items() -> None:
    parsed = parse(reply(item(), item("works on AETHER", category="project", privacy="public")))
    assert parsed.dropped == 0
    assert [(p.category, p.privacy) for p in parsed.proposals] == [
        ("preference", "personal"),
        ("project", "public"),
    ]


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```", "  ```JSON\n{}\n```  "])
def test_parse_tolerates_one_code_fence(fence: str) -> None:
    parsed = parse(fence.replace("{}", reply(item())))
    assert (len(parsed.proposals), parsed.dropped) == (1, 0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Sure! Here are the facts: " + reply(item()),
        reply(item()) + " hope that helps",
        "```python\n" + reply(item()) + "\n```",
        "[]",
        json.dumps({"proposals": {}}),
        json.dumps({"proposals": [], "approved": True}),
        "{" * 5000,
        "not json at all",
    ],
)
def test_anything_else_is_one_dropped_item(text: str) -> None:
    assert parse(text) == Parsed((), 1)


@pytest.mark.parametrize(
    "bad",
    [
        item(privacy="secret"),
        item(privacy="top"),
        item(category="password"),
        item(content=""),
        item(content="   "),
        item(content=42),
        {**item(), "status": "approved"},
        {**item(), "key": "k"},
        {"content": "x", "category": "project"},
        "just a string",
        None,
    ],
)
def test_bad_items_are_dropped_and_the_rest_kept(bad: Any) -> None:
    parsed = parse(reply(bad, item()))
    assert (len(parsed.proposals), parsed.dropped) == (1, 1)


def test_at_most_three_and_no_duplicates() -> None:
    items = [item(f"fact {i}") for i in range(MAX_PROPOSALS + 2)]
    parsed = parse(reply(item("fact 0"), *items, item("FACT 1")))
    assert [p.content for p in parsed.proposals] == ["fact 0", "fact 1", "fact 2"]
    assert parsed.dropped == len(items) + 2 - MAX_PROPOSALS


def test_empty_list_is_nothing_dropped() -> None:
    assert parse(reply()) == Parsed((), 0)


def test_content_stays_out_of_repr() -> None:
    assert CANARY not in repr(parse(reply(item())))


# --- level floor and the request ------------------------------------------------------


@pytest.mark.parametrize(
    ("suggested", "floor", "stored"),
    [
        ("public", "personal", "personal"),
        ("personal", "public", "personal"),
        ("sensitive", "personal", "sensitive"),
        ("public", "sensitive", "sensitive"),
        ("public", "public", "public"),
    ],
)
def test_the_level_is_never_below_the_request(suggested: Any, floor: Any, stored: Any) -> None:
    assert floored(suggested, floor) == stored


def test_the_proposal_request_has_only_the_latest_user_message() -> None:
    request = ModelRequest(
        messages=(
            Message(role="user", content="first"),
            Message(role="assistant", content=f"{CANARY} model claim"),
            Message(role="user", content="I drink green tea"),
            Message(role="assistant", content="noted"),
        ),
        privacy="sensitive",
    )
    prop = proposal_request(request)
    assert prop is not None
    assert [(m.role, m.content) for m in prop.messages] == [("user", "I drink green tea")]
    assert prop.system == INSTRUCTIONS
    assert prop.privacy == "sensitive"
    assert (prop.max_tokens, prop.temperature) == (PROPOSAL_MAX_TOKENS, 0.0)


def test_no_user_message_means_no_proposal_call() -> None:
    request = ModelRequest(messages=(Message(role="assistant", content="hi"),))
    assert proposal_request(request) is None


# --- storing -----------------------------------------------------------------------


def _store(parsed: Parsed, store: FakeStore, floor: Any = "personal") -> Any:
    return asyncio.run(
        store_proposals(
            store,  # type: ignore[arg-type]
            parsed,
            floor=floor,
            provider="ollama",
            request_id="req-1",
        )
    )


def test_proposals_are_stored_as_model_records_with_provenance() -> None:
    store = FakeStore()
    parsed = Parsed((Proposal("tea", "preference", "public"),), 2)

    stored = _store(parsed, store)

    assert (stored.proposed, stored.dropped) == (1, 2)
    new = store.created[0]
    assert (new.source, new.source_provider, new.source_request_id) == ("model", "ollama", "req-1")
    assert new.privacy == "personal"  # floored
    assert new.key is None and new.expires_at is None


def test_a_refused_record_is_dropped_and_the_limit_drops_the_rest() -> None:
    proposals = tuple(Proposal(f"fact {i}", "project", "personal") for i in range(3))
    store = FakeStore(fail={0: SecretContent(), 1: PendingLimit()})

    stored = _store(Parsed(proposals, 0), store)

    assert (stored.proposed, stored.dropped) == (0, 3)


def test_an_over_long_proposal_is_dropped_before_the_store() -> None:
    store = FakeStore()
    stored = _store(Parsed((Proposal("x" * 5000, "project", "personal"),), 0), store)
    assert (stored.proposed, stored.dropped, store.created) == (0, 1, [])


def test_a_database_failure_propagates() -> None:
    store = FakeStore(fail={0: StoreUnavailable("down")})
    with pytest.raises(StoreUnavailable):
        _store(Parsed((Proposal("tea", "project", "personal"),), 0), store)


# --- Router.call_once -------------------------------------------------------------------


@pytest.fixture
def identity() -> Identity:
    return load_identity()


def _router(identity: Identity, *providers: FakeProvider, source: Any = None) -> Router:
    return Router(
        list(providers), identity, secret_check=CHECK, probe=lambda: snapshot(4.0), memory=source
    )


def _req(privacy: Any = "personal") -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content="hi"),), privacy=privacy)


def test_call_once_sends_no_memory(identity: Identity) -> None:
    local = FakeProvider("ollama", "local", memory_limit="sensitive")
    source = FakeSource([record("public")])
    r = _router(identity, local, source=source)

    asyncio.run(r.call_once("ollama", _req(), RouteOptions()))

    assert source.asked == []
    assert MEMORY_HEADING not in (local.requests[0].system or "")


@pytest.mark.parametrize(
    ("name", "privacy", "allow_cloud"),
    [
        ("claude", "personal", False),  # the cloud needs allow_cloud
        ("claude", "sensitive", True),  # above the cloud's max
        ("ollama", "secret", True),  # never sent
        ("nobody", "public", True),  # unknown provider
    ],
)
def test_call_once_refuses_what_routing_would_refuse(
    identity: Identity, name: str, privacy: Any, allow_cloud: bool
) -> None:
    local = FakeProvider("ollama", "local")
    cloud = FakeProvider("claude", "cloud")
    r = _router(identity, local, cloud)

    with pytest.raises(NoRoute):
        asyncio.run(r.call_once(name, _req(privacy), RouteOptions(allow_cloud=allow_cloud)))

    assert local.calls == cloud.calls == 0


# --- /chat ---------------------------------------------------------------------------


def test_without_propose_memory_nothing_changes() -> None:
    store = FakeStore()
    setup = Setup(store=store)

    body = setup.post().json()

    assert body["memory_proposals"] is None
    assert setup.local.calls == 1 and store.created == []


def test_propose_memory_stores_pending_proposals_from_the_answering_provider() -> None:
    store = FakeStore()
    setup = Setup(store=store, local_replies=["the answer", reply(item(), item("second"))])

    response = setup.post(propose_memory=True, privacy="public")

    body = response.json()
    assert response.status_code == 200
    assert body["text"] == "the answer"
    assert body["memory_proposals"] == 2
    assert setup.local.calls == 2 and setup.cloud.calls == 0
    second = setup.local.requests[1]
    assert [m.content for m in second.messages] == [PROMPT]
    assert INSTRUCTIONS in (second.system or "")
    assert [n.source for n in store.created] == ["model", "model"]
    assert all(n.source_provider == "ollama" for n in store.created)
    # Both calls have an event; no proposal text in any event or log line.
    kinds = [r["event_type"] for r in setup.rows]
    assert kinds.count("model.call.completed") == 2
    assert CANARY not in json.dumps(setup.rows, default=str)
    assert CANARY not in setup.log.getvalue()
    lines = [r for r in setup.log_records() if r["message"] == "Memory proposals"]
    assert [(r["proposed"], r["dropped"], r["error_type"]) for r in lines] == [(2, 0, None)]


def test_the_cloud_proposes_only_with_allow_cloud_and_gets_no_memory() -> None:
    store = FakeStore()
    source = FakeSource([record("public")])
    setup = Setup(
        store=store,
        memory=source,
        cloud_memory_limit="personal",
        cloud_replies=["cloud answer", reply(item())],
    )

    body = setup.post(propose_memory=True, allow_cloud=True, override="claude").json()

    assert body["memory_proposals"] == 1
    assert setup.cloud.calls == 2
    assert MEMORY_HEADING in (setup.cloud.requests[0].system or "")
    assert MEMORY_HEADING not in (setup.cloud.requests[1].system or "")
    assert len(source.asked) == 1  # only the answer's call read memory


@pytest.mark.parametrize("finish", ["refusal", "length"])
def test_no_proposal_call_after_a_refusal_or_a_cut_off_answer(finish: Any) -> None:
    store = FakeStore()
    setup = Setup(store=store, finish_reason=finish)

    body = setup.post(propose_memory=True).json()

    assert body["memory_proposals"] == 0
    assert setup.local.calls == 1 and store.created == []


def test_a_failed_proposal_call_never_fails_the_answer() -> None:
    store = FakeStore()
    setup = Setup(store=store, local_replies=["the answer"])
    error = ProviderUnavailable("ollama", "connection refused")

    async def fail_second(request: ModelRequest) -> Any:
        if setup.local.calls >= 1:
            setup.local.calls += 1
            raise error
        return await original(request)

    original = setup.local._generate
    setup.local._generate = fail_second  # type: ignore[method-assign]

    response = setup.post(propose_memory=True)

    assert response.status_code == 200
    assert response.json()["memory_proposals"] == 0
    assert setup.cloud.calls == 0  # no failover for proposals
    kinds = [r["event_type"] for r in setup.rows]
    assert "provider.unavailable" in kinds and "request.failed" not in kinds


@pytest.mark.parametrize(
    "text", ["I will remember that!", reply(item(privacy="secret")), "```\n" + reply()]
)
def test_an_unusable_reply_stores_nothing(text: str) -> None:
    store = FakeStore()
    setup = Setup(store=store, local_replies=["the answer", text])

    body = setup.post(propose_memory=True).json()

    assert body["memory_proposals"] == 0 and store.created == []


def test_a_model_cannot_lower_the_requests_level() -> None:
    store = FakeStore()
    setup = Setup(store=store, local_replies=["a", reply(item(privacy="public"))])

    setup.post(propose_memory=True, privacy="sensitive", include_sensitive=True)

    assert [n.privacy for n in store.created] == ["sensitive"]


def test_without_a_store_proposals_are_zero_and_no_call_is_made() -> None:
    setup = Setup(local_replies=["a", reply(item())])

    body = setup.post(propose_memory=True).json()

    assert body["memory_proposals"] == 0 and setup.local.calls == 1


def test_a_store_outage_never_fails_the_answer() -> None:
    store = FakeStore(fail={0: StoreUnavailable("down")})
    setup = Setup(store=store, local_replies=["a", reply(item())])

    response = setup.post(propose_memory=True)

    assert response.status_code == 200 and response.json()["memory_proposals"] == 0
    line = next(r for r in setup.log_records() if r["message"] == "Memory proposals")
    assert line["error_type"] == "StoreUnavailable"


def test_proposal_ids_are_never_approved_by_a_model() -> None:
    # Whatever the reply claims, the record is a model proposal (pending).
    store = FakeStore()
    claim = {"content": "approved by owner", "category": "profile", "privacy": "public"}
    setup = Setup(store=store, local_replies=["a", reply(claim)])

    setup.post(propose_memory=True)

    assert [n.source for n in store.created] == ["model"]
