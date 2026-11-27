"""Memory in model calls (ADR-024 §3, §5; step 12 of #58, #2 part 2). No database:
a fake ``MemorySource`` runs the real ``select_memory`` over in-memory records.

All content is invented canaries.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from core.identity import Identity, load_identity
from memory.retrieval import PREAMBLE, select_memory
from memory.store import Candidate, MemoryRecord
from router.contract import (
    NON_SECRET_LEVELS,
    MemoryNotAllowed,
    Message,
    ModelRequest,
    PrivacyLevel,
    PrivacyNotAllowed,
    ProviderInfo,
    ProviderUnavailable,
    privacy_rank,
)
from router.echo import REMOVED_ECHO
from router.identity_call import apply_identity, call_with_identity
from router.memory_context import (
    NO_MEMORY,
    MemoryContext,
    check_memory,
    effective_privacy,
    fetch_level,
)
from router.routing import MemoryUse, RouteFailed, RouteOptions, Router
from tests.chat_helpers import PROMPT, Setup
from tests.fakes import CHECK, FakeProvider, snapshot

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
CANARY = "canary-memchat-9b3e"


def record(level: PrivacyLevel, n: int | str = 1, **changes: Any) -> MemoryRecord:
    values: dict[str, Any] = {
        "id": uuid4(),
        "content": f"{CANARY} {level} fact {n}",
        "key": None,
        "category": "project",
        "privacy": level,
        "status": "approved",
        "source": "owner",
        "source_provider": None,
        "source_request_id": None,
        "privacy_source": "owner",
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(days=1),
        "approved_at": NOW - timedelta(days=1),
        "expires_at": None,
    }
    return MemoryRecord(**{**values, **changes})


@dataclass
class Asked:
    text: str | None
    limit: PrivacyLevel
    include_sensitive: bool


class FakeSource:
    """Reads like the store (level <= limit), selects with the real function."""

    def __init__(self, records: list[MemoryRecord], error: Exception | None = None) -> None:
        self.records = records
        self.error = error
        self.asked: list[Asked] = []
        self.sizes: list[tuple[int, int]] = []

    async def context_for(
        self,
        request_text: str | None,
        *,
        limit: PrivacyLevel,
        include_sensitive: bool,
        max_records: int = 20,
        max_chars: int = 4000,
    ) -> MemoryContext:
        self.asked.append(Asked(request_text, limit, include_sensitive))
        self.sizes.append((max_records, max_chars))
        if self.error is not None:
            raise self.error
        candidates = [
            Candidate(r, False, 0.0)
            for r in self.records
            if privacy_rank(r.privacy) <= privacy_rank(limit)
        ]
        return select_memory(
            candidates,
            limit=limit,
            include_by_default=("public", "personal"),
            include_sensitive=include_sensitive,
            now=NOW,
            max_records=max_records,
            max_chars=max_chars,
        ).to_context()


class WrongSource:
    """A deliberately wrong selection: always the same context, whatever the limit."""

    def __init__(self, memory: MemoryContext) -> None:
        self.memory = memory

    async def context_for(
        self,
        request_text: str | None,
        *,
        limit: PrivacyLevel,
        include_sensitive: bool,
        max_records: int = 20,
        max_chars: int = 4000,
    ) -> MemoryContext:
        return self.memory


def context_of(*levels: PrivacyLevel) -> MemoryContext:
    recs = [record(level, i) for i, level in enumerate(levels)]
    return MemoryContext(
        record_ids=tuple(r.id for r in recs),
        levels=levels,
        context="\n".join([PREAMBLE, *(f"- (project, {r.privacy}) {r.content}" for r in recs)]),
        texts=tuple(r.content for r in recs),
        chars=10,
    )


ALL = [record("public"), record("personal"), record("sensitive")]


def local(limit: PrivacyLevel | None = "sensitive", **kw: Any) -> FakeProvider:
    return FakeProvider("ollama", "local", memory_limit=limit, **kw)


# The cloud fakes here default to a *public* memory limit on purpose: they test
# that a configured limit is applied, whatever it is. Claude's real limit
# (personal, ADR-024 D3) is tested in test_memory_recall.py and below.
def cloud(limit: PrivacyLevel | None = "public", **kw: Any) -> FakeProvider:
    return FakeProvider("claude", "cloud", memory_limit=limit, **kw)


def router(
    providers: list[FakeProvider], source: Any, identity: Identity, free_gib: float = 4.0
) -> Router:
    return Router(
        providers, identity, secret_check=CHECK, probe=lambda: snapshot(free_gib), memory=source
    )


def request(text: str = "what about the canary?", privacy: PrivacyLevel = "public") -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),), privacy=privacy)


def handle(r: Router, req: ModelRequest, options: RouteOptions, uses: list[MemoryUse]) -> Any:
    return asyncio.run(r.handle(req, options, on_memory=uses.append))


def sent_levels(provider: FakeProvider) -> list[list[PrivacyLevel]]:
    """For each call: the levels of the memory lines in its system prompt."""
    out = []
    for req in provider.requests:
        system = req.system or ""
        out.append(sorted(lvl for lvl in NON_SECRET_LEVELS if f"{CANARY} {lvl} fact" in system))
    return out


# --- per call selection ---------------------------------------------------------------


def test_local_gets_default_levels_and_the_effective_privacy(identity: Identity) -> None:
    p = local()
    src = FakeSource(list(ALL))
    uses: list[MemoryUse] = []

    handle(router([p], src, identity), request(privacy="public"), RouteOptions(), uses)

    assert sent_levels(p) == [["personal", "public"]]
    sent = p.requests[0]
    assert sent.privacy == "personal"  # raised from public by the memory
    assert PREAMBLE in (sent.system or "")
    assert all(CANARY not in m.content for m in sent.messages)  # never in the messages
    # Without include_sensitive the read stops below sensitive (#140).
    assert [(a.limit, a.include_sensitive) for a in src.asked] == [("personal", False)]
    assert [(u.provider, u.effective_privacy, len(u.memory.record_ids)) for u in uses] == [
        ("ollama", "personal", 2)
    ]


def test_include_sensitive_reaches_a_trusted_local_host(identity: Identity) -> None:
    p = local()
    src = FakeSource(list(ALL))

    handle(router([p], src, identity), request(), RouteOptions(include_sensitive=True), [])

    assert sent_levels(p) == [["personal", "public", "sensitive"]]
    assert p.requests[0].privacy == "sensitive"
    assert src.asked[0].limit == "sensitive"


def test_default_size_limits_reach_the_source(identity: Identity) -> None:
    src = FakeSource(list(ALL))
    handle(router([local()], src, identity), request(), RouteOptions(), [])
    assert src.sizes == [(20, 4000)]


def test_a_providers_lower_size_limits_reach_the_selection(identity: Identity) -> None:
    # A small local model gets fewer records (ADR-024 D9, #143).
    p = local(memory_max_records=1, memory_max_chars=3000)
    src = FakeSource(list(ALL))
    uses: list[MemoryUse] = []

    handle(router([p], src, identity), request(), RouteOptions(include_sensitive=True), uses)

    assert src.sizes == [(1, 3000)]
    assert len(sent_levels(p)[0]) == 1
    assert len(uses[0].memory.record_ids) == 1
    assert uses[0].memory.left_out == 2


def test_failover_uses_the_next_providers_own_size_limits(identity: Identity) -> None:
    small = local(memory_max_records=1, error=ProviderUnavailable("ollama", "down"))
    big = cloud("personal")
    src = FakeSource(list(ALL))

    handle(router([small, big], src, identity), request(), RouteOptions(allow_cloud=True), [])

    assert src.sizes == [(1, 4000), (20, 4000)]


@pytest.mark.parametrize("field", ["memory_max_records", "memory_max_chars"])
@pytest.mark.parametrize("value", [0, -1, 10**6])
def test_provider_size_limits_may_only_be_lowered(field: str, value: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProviderInfo.model_validate(
            {"name": "x", "kind": "local", "default_model": "m", field: value}
        )


def test_a_local_host_limited_to_public_gets_public_only(identity: Identity) -> None:
    p = local("public")

    handle(
        router([p], FakeSource(list(ALL)), identity),
        request(),
        RouteOptions(include_sensitive=True),
        [],
    )

    assert sent_levels(p) == [["public"]]


def test_cloud_gets_public_only_even_with_include_sensitive(identity: Identity) -> None:
    p = cloud()

    handle(
        router([p], FakeSource(list(ALL)), identity, free_gib=0),
        request(),
        RouteOptions(allow_cloud=True, override="claude", include_sensitive=True),
        [],
    )

    assert sent_levels(p) == [["public"]]
    assert p.requests[0].privacy == "public"


def test_a_provider_without_a_memory_limit_gets_nothing_and_no_event(identity: Identity) -> None:
    p = local(None)
    src = FakeSource(list(ALL))
    uses: list[MemoryUse] = []

    handle(router([p], src, identity), request(), RouteOptions(), uses)

    assert src.asked == [] and uses == []
    assert CANARY not in (p.requests[0].system or "")


def test_without_a_source_nothing_changes(identity: Identity) -> None:
    p = local()
    uses: list[MemoryUse] = []

    handle(router([p], None, identity), request(privacy="public"), RouteOptions(), uses)

    assert uses == [] and p.requests[0].privacy == "public"
    assert "Relevant memory:" not in (p.requests[0].system or "")


def test_empty_selection_still_reports_and_adds_no_section(identity: Identity) -> None:
    p = local()
    uses: list[MemoryUse] = []

    handle(router([p], FakeSource([]), identity), request(), RouteOptions(), uses)

    assert len(uses) == 1 and uses[0].memory.record_ids == ()
    assert "Relevant memory:" not in (p.requests[0].system or "")


def test_search_text_is_the_latest_user_message(identity: Identity) -> None:
    src = FakeSource([])
    req = ModelRequest(
        messages=(
            Message(role="user", content="first question"),
            Message(role="assistant", content="an answer"),
            Message(role="user", content="second question"),
            Message(role="assistant", content="pending"),
        )
    )

    handle(router([local()], src, identity), req, RouteOptions(), [])

    assert src.asked[0].text == "second question"


def test_failing_source_means_no_memory_and_a_metadata_warning(
    identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    p = local()
    src = FakeSource(list(ALL), error=RuntimeError(f"db down {CANARY}"))
    uses: list[MemoryUse] = []

    with caplog.at_level(logging.WARNING, logger="aether.router.routing"):
        out = handle(router([p], src, identity), request(), RouteOptions(), uses)

    assert out.response.text == "answer from ollama"
    assert uses == [] and CANARY not in (p.requests[0].system or "")
    warned = [r for r in caplog.records if r.getMessage() == "Memory retrieval failed"]
    assert len(warned) == 1 and getattr(warned[0], "error_type", None) == "RuntimeError"
    assert CANARY not in caplog.text


def test_a_failing_memory_hook_does_not_break_the_call(identity: Identity) -> None:
    def boom(_: MemoryUse) -> None:
        raise RuntimeError("hook")

    r = router([local()], FakeSource(list(ALL)), identity)

    out = asyncio.run(r.handle(request(), RouteOptions(), on_memory=boom))

    assert out.response.provider == "ollama"


def test_memory_never_changes_the_route(identity: Identity) -> None:
    # Request public, memory up to sensitive: still routed by the request's own level.
    p, c = local(), cloud()
    uses: list[MemoryUse] = []

    out = handle(
        router([p, c], FakeSource(list(ALL)), identity, free_gib=0.1),
        request(privacy="public"),
        RouteOptions(allow_cloud=True, include_sensitive=True),
        uses,
    )

    assert out.decision.provider == "claude"
    assert sent_levels(c) == [["public"]] and p.calls == 0


# --- failover ---------------------------------------------------------------------


def test_failover_to_cloud_reselects_and_drops_private_memory(identity: Identity) -> None:
    p = local(error=ProviderUnavailable("ollama", "down"))
    c = cloud()
    src = FakeSource(list(ALL))
    uses: list[MemoryUse] = []

    out = handle(
        router([p, c], src, identity),
        request(privacy="public"),
        RouteOptions(allow_cloud=True, include_sensitive=True),
        uses,
    )

    assert out.response.provider == "claude" and out.failed_first is not None
    assert sent_levels(p) == [["personal", "public", "sensitive"]]
    assert sent_levels(c) == [["public"]]
    assert c.requests[0].privacy == "public"
    assert [a.limit for a in src.asked] == ["sensitive", "public"]
    assert [(u.provider, u.effective_privacy) for u in uses] == [
        ("ollama", "sensitive"),
        ("claude", "public"),
    ]


def test_failover_without_allow_cloud_never_reads_for_the_cloud(identity: Identity) -> None:
    p = local(error=ProviderUnavailable("ollama", "down"))
    src = FakeSource(list(ALL))

    with pytest.raises(RouteFailed):
        handle(router([p, cloud()], src, identity), request(), RouteOptions(), [])

    assert len(src.asked) == 1


# --- the call_with_identity backstop -------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "levels", "allow_cloud", "include_sensitive"),
    [
        ("cloud", ("personal",), True, False),  # personal to a cloud limited to public
        ("cloud", ("sensitive",), True, True),  # sensitive to the cloud, ever
        ("cloud", ("public",), False, False),  # cloud without allow_cloud
        ("public-local", ("personal",), False, False),  # above a local public limit
        ("off-network", ("public",), False, False),  # off-network without allow_cloud
        ("off-network", ("sensitive",), True, True),  # sensitive off-network, ever
        ("trusted", ("sensitive",), False, False),  # sensitive without include_sensitive
        ("none", ("public",), False, False),  # a provider without a memory limit
        ("trusted", ("public", "sensitive"), False, False),  # one bad record is enough
    ],
)
def test_backstop_refuses_a_wrong_selection_before_sending(
    identity: Identity,
    provider: str,
    levels: tuple[PrivacyLevel, ...],
    allow_cloud: bool,
    include_sensitive: bool,
) -> None:
    p = {
        "cloud": cloud(),
        "public-local": local("public"),
        "off-network": FakeProvider("ollama", "local", memory_limit="personal", off_network=True),
        "trusted": local("sensitive"),
        "none": local(None),
    }[provider]

    with pytest.raises(MemoryNotAllowed) as caught:
        asyncio.run(
            call_with_identity(
                p,
                request(),
                identity,
                secret_check=CHECK,
                memory=context_of(*levels),
                allow_cloud=allow_cloud,
                include_sensitive=include_sensitive,
            )
        )

    assert p.calls == 0
    assert caught.value.retryable is False
    assert caught.value.message == MemoryNotAllowed.MESSAGE


def test_backstop_defaults_are_the_safe_ones(identity: Identity) -> None:
    c = cloud()

    with pytest.raises(MemoryNotAllowed):
        asyncio.run(
            call_with_identity(
                c, request(), identity, secret_check=CHECK, memory=context_of("public")
            )
        )
    assert c.calls == 0


def test_backstop_allows_a_right_selection(identity: Identity) -> None:
    p = local("sensitive")

    asyncio.run(
        call_with_identity(
            p,
            request(),
            identity,
            secret_check=CHECK,
            memory=context_of("public", "sensitive"),
            include_sensitive=True,
        )
    )

    assert p.calls == 1 and p.requests[0].privacy == "sensitive"


def test_backstop_through_the_router_is_final(identity: Identity) -> None:
    # The source ignores the limit: the backstop refuses, and there is no failover.
    p, c = local("public"), cloud()

    with pytest.raises(RouteFailed) as caught:
        handle(
            router([p, c], WrongSource(context_of("personal")), identity),
            request(),
            RouteOptions(allow_cloud=True),
            [],
        )

    assert p.calls == 0 and c.calls == 0
    assert [a.error_type for a in caught.value.attempts] == ["MemoryNotAllowed"]


def test_memory_limit_above_max_privacy_cannot_be_configured() -> None:
    with pytest.raises(ValueError, match="memory_limit"):
        FakeProvider("claude", "cloud", memory_limit="sensitive")


def test_generate_refuses_sensitive_memory_to_the_cloud_if_the_backstop_is_bypassed(
    identity: Identity,
) -> None:
    # Second layer (ADR-024 §8): the effective level reaches generate(), which
    # refuses it against the cloud's max_privacy without sending anything.
    c = cloud()
    prepared = apply_identity(request(privacy="public"), identity, c.name, context_of("sensitive"))

    assert prepared.privacy == "sensitive"
    with pytest.raises(PrivacyNotAllowed):
        asyncio.run(c.generate(prepared))
    assert c.calls == 0


def test_a_slow_source_is_given_up_and_the_call_goes_ahead(
    identity: Identity, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import router.routing as routing

    class Slow(FakeSource):
        async def context_for(
            self,
            request_text: str | None,
            *,
            limit: PrivacyLevel,
            include_sensitive: bool,
            **_sizes: int,
        ) -> MemoryContext:
            await asyncio.sleep(5)
            raise AssertionError("not reached")

    monkeypatch.setattr(routing, "MEMORY_TIMEOUT_S", 0.05)
    p = local()

    with caplog.at_level(logging.WARNING, logger="aether.router.routing"):
        out = handle(router([p], Slow([]), identity), request(), RouteOptions(), [])

    assert out.response.provider == "ollama"
    assert "Relevant memory:" not in (p.requests[0].system or "")
    warned = [r for r in caplog.records if r.getMessage() == "Memory retrieval failed"]
    assert getattr(warned[0], "error_type", None) == "TimeoutError"


@pytest.mark.parametrize("level", ["public", "personal"])
def test_tripwire_checks_memory_text_for_cloud(identity: Identity, level: PrivacyLevel) -> None:
    from router.contract import SecretContentRefused

    key = "sk-ant-api03-FAKE-memory-tripwire-0123456789"  # gitleaks:allow
    rec = record(level, content=f"{CANARY} {key}")
    c = cloud("personal")

    with pytest.raises(SecretContentRefused):
        asyncio.run(
            call_with_identity(
                c,
                request(),
                identity,
                secret_check=CHECK,
                memory=context_from([rec]),
                allow_cloud=True,
            )
        )
    assert c.calls == 0


def context_from(records: list[MemoryRecord]) -> MemoryContext:
    default: tuple[PrivacyLevel, ...] = ("public", "personal")
    return select_memory(
        [Candidate(r, False, 0.0) for r in records],
        limit="personal",
        include_by_default=default,
        include_sensitive=False,
        now=NOW,
    ).to_context()


# --- echo backstop ------------------------------------------------------------------


def test_error_quoting_memory_text_is_removed_everywhere(
    identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    rec = record("public", content=f"{CANARY} public quoted fact")
    error = ProviderUnavailable("ollama", f"upstream said: {rec.content}")
    p = local(error=error)
    c = cloud(error=ProviderUnavailable("claude", "also down"))

    with caplog.at_level(logging.DEBUG), pytest.raises(RouteFailed) as caught:
        handle(router([p, c], FakeSource([rec]), identity), request(), RouteOptions(), [])

    assert caught.value.attempts[0].message == REMOVED_ECHO
    assert CANARY not in str(error) and error.message == REMOVED_ECHO
    assert CANARY not in caplog.text


def test_error_quoting_request_text_is_removed_from_logs(
    identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    text = f"{CANARY} the owner's question"
    p = local(None, error=ProviderUnavailable("ollama", f"bad prompt {text}"))

    with caplog.at_level(logging.DEBUG), pytest.raises(RouteFailed) as caught:
        handle(router([p], None, identity), request(text), RouteOptions(), [])

    assert caught.value.attempts[0].message == REMOVED_ECHO
    # The request text appears in no log record (the message is never logged).
    assert CANARY not in caplog.text


# --- small parts --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "include_sensitive", "expected"),
    [
        ("public", False, "public"),
        ("personal", False, "personal"),
        ("sensitive", False, "personal"),
        ("sensitive", True, "sensitive"),
        ("public", True, "public"),
    ],
)
def test_fetch_level(limit: PrivacyLevel, include_sensitive: bool, expected: str) -> None:
    assert fetch_level(limit, include_sensitive=include_sensitive) == expected


def test_memory_context_shape_and_repr() -> None:
    with pytest.raises(ValueError, match="one level per record"):
        MemoryContext(record_ids=(uuid4(),), levels=(), context="x", texts=())
    with pytest.raises(ValueError, match="context"):
        MemoryContext(record_ids=(), levels=(), context="x", texts=())
    with pytest.raises(ValueError, match="context"):
        MemoryContext(record_ids=(uuid4(),), levels=("public",), context=None, texts=())
    ctx = context_of("public", "personal")
    assert CANARY not in repr(ctx)
    assert ctx.count_by_level() == {"public": 1, "personal": 1, "sensitive": 0}
    assert NO_MEMORY.highest_level is None


def test_effective_privacy() -> None:
    assert effective_privacy("public", None) == "public"
    assert effective_privacy("public", NO_MEMORY) == "public"
    assert effective_privacy("public", context_of("sensitive")) == "sensitive"
    assert effective_privacy("sensitive", context_of("public")) == "sensitive"


def test_check_memory_needs_real_booleans() -> None:
    with pytest.raises(TypeError):
        check_memory(
            local().info,
            context_of("public"),
            allow_cloud="no",  # type: ignore[arg-type]
            include_sensitive=False,
        )


# --- /chat ---------------------------------------------------------------------------


def _chat_setup(records: list[MemoryRecord], **kw: Any) -> Setup:
    return Setup(
        memory=FakeSource(records),
        local_memory_limit="sensitive",
        cloud_memory_limit="public",
        **kw,
    )


def test_chat_records_one_retrieved_event_per_call() -> None:
    recs = [record("public"), record("personal"), record("sensitive")]
    s = _chat_setup(recs)

    resp = s.post(privacy="public")

    assert resp.status_code == 200
    rows = [r for r in s.rows if r["event_type"] == "memory.context.retrieved"]
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "ollama" and row["actor"] == "memory"
    assert (row["memory_count_public"], row["memory_count_personal"]) == (1, 1)
    assert row["memory_count_sensitive"] == 0
    assert row["memory_effective_level"] == "personal"
    assert set(row["memory_record_ids"]) == {recs[0].id, recs[1].id}
    assert row["memory_left_out"] == 0  # sensitive was never read (#140)
    assert row["request_id"] == resp.json()["request_id"]
    # The other events keep the request's own level.
    selected = [r for r in s.rows if r["event_type"] == "model.route.selected"]
    assert selected[0]["privacy"] == "public"
    assert all(CANARY not in str(v) for r in s.rows for v in r.values())


def test_chat_include_sensitive_is_passed_and_defaults_off() -> None:
    s = _chat_setup([record("sensitive")])

    s.post()
    s.post(include_sensitive=True)

    assert sent_levels(s.local) == [[], ["sensitive"]]


def test_chat_include_sensitive_must_be_a_boolean() -> None:
    s = _chat_setup([])

    assert s.post(include_sensitive={"x": 1}).status_code == 422


def test_chat_failover_records_both_selections() -> None:
    s = _chat_setup(
        [record("public"), record("personal")],
        local_error=ProviderUnavailable("ollama", "down"),
    )

    resp = s.post(allow_cloud=True)

    assert resp.status_code == 200 and resp.json()["provider"] == "claude"
    rows = [r for r in s.rows if r["event_type"] == "memory.context.retrieved"]
    assert [(r["provider"], r["memory_effective_level"]) for r in rows] == [
        ("ollama", "personal"),
        ("claude", "personal"),  # the request's own level is personal (the default)
    ]
    assert [r["memory_count_personal"] for r in rows] == [1, 0]
    assert sent_levels(s.cloud) == [["public"]]


def test_chat_error_body_and_events_never_echo_memory_text() -> None:
    rec = record("public", content=f"{CANARY} echoed public fact")
    s = _chat_setup(
        [rec],
        local_error=ProviderUnavailable("ollama", f"upstream: {rec.content}"),
    )

    resp = s.post()

    assert resp.status_code == 502
    assert CANARY not in resp.text
    assert REMOVED_ECHO in resp.text
    assert all(CANARY not in str(v) for r in s.rows for v in r.values())
    assert CANARY not in s.log.getvalue()


def test_chat_events_echo_check_covers_memory_text() -> None:
    # Even an event field built from somewhere else is blanked if it quotes memory.
    from core.events import forbidden_texts

    rec = record("public", content=f"{CANARY} forbidden fact")
    texts = forbidden_texts(request(PROMPT), [rec.content, "short"])

    assert rec.content in texts and "short" not in texts and PROMPT in texts


def test_chat_without_memory_source_has_no_retrieved_event() -> None:
    s = Setup()

    assert s.post().status_code == 200
    assert not [r for r in s.rows if r["event_type"] == "memory.context.retrieved"]


def test_create_app_wires_the_memory_store_into_the_router(identity: Identity) -> None:
    from pydantic import SecretStr

    from core.app import create_app
    from core.config import Settings
    from core.events import EventRecorder
    from memory.retrieval import MemoryRetriever
    from memory.store import MemoryStore

    settings = Settings(_env_file=None, aether_api_token=SecretStr("t" * 40))
    store = MemoryStore.__new__(MemoryStore)  # never touches a database here

    app = create_app(settings, load_identity(), events=EventRecorder(None), memory=store)
    without = create_app(settings, load_identity(), events=EventRecorder(None))

    assert isinstance(app.state.router._memory, MemoryRetriever)
    assert without.state.router._memory is None
