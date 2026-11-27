"""An untrusted Ollama host is treated like the cloud for privacy (ADR-032, #22,
step 13 of #58): it needs ``allow_cloud`` for requests and memory, gets memory up
to personal, and gets the #8 tripwire. It stays ``kind="local"``, so it is still
preferred over the cloud when it has headroom. The owner's opt-in
(``AETHER_OLLAMA_TRUST_HOST``) turns all of it off.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from core.identity import Identity
from router.contract import (
    Message,
    ModelRequest,
    PrivacyLevel,
    ProviderInfo,
    ProviderUnavailable,
    SecretContentRefused,
)
from router.ollama import OllamaProvider
from router.routing import NoRoute, RouteFailed, RouteOptions, Router
from tests.fakes import CHECK, FakeProvider, snapshot
from tests.test_memory_chat import ALL, CANARY, FakeSource, sent_levels
from tests.test_ollama_host_trust import MODEL, UNTRUSTED_HOST_MARKER, handler

UNTRUSTED = f"https://{UNTRUSTED_HOST_MARKER}"
TRUSTED = "http://127.0.0.1:11434"
FAKE_KEY = "sk-ant-api03-FAKE-untrusted-tripwire-0123456789"  # gitleaks:allow


def ollama(host: str, seen: list[httpx.Request], **kw: Any) -> OllamaProvider:
    return OllamaProvider(host, MODEL, transport=handler(seen), **kw)


def chats(seen: list[httpx.Request]) -> list[dict[str, Any]]:
    return [json.loads(q.content) for q in seen if q.url.path == "/api/chat"]


def request(privacy: PrivacyLevel = "personal", text: str = "hello there") -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),), privacy=privacy)


def router(providers: list[Any], identity: Identity, free_gib: float = 4.0, **kw: Any) -> Router:
    return Router(providers, identity, secret_check=CHECK, probe=lambda: snapshot(free_gib), **kw)


# --- provider info -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "trust", "off_network"),
    [
        (TRUSTED, False, False),
        ("http://192.168.1.20:11434", False, False),
        (UNTRUSTED, False, True),
        ("http://8.8.8.8:11434", False, True),
        ("http://100.64.0.1:11434", False, True),
        (UNTRUSTED, True, False),  # the owner's opt-in
    ],
)
def test_off_network_follows_host_trust(host: str, trust: bool, off_network: bool) -> None:
    info = ollama(host, [], trust_host=trust).info

    assert info.off_network is off_network
    assert info.needs_allow_cloud is off_network
    assert info.kind == "local"


def test_off_network_is_only_for_a_capped_local_provider() -> None:
    base: dict[str, Any] = {"name": "p", "default_model": "m"}
    with pytest.raises(ValueError, match="off_network"):
        ProviderInfo(**base, kind="cloud", max_privacy="personal", off_network=True)
    with pytest.raises(ValueError, match="off_network"):
        ProviderInfo(**base, kind="local", max_privacy="sensitive", off_network=True)
    ok = ProviderInfo(**base, kind="local", max_privacy="personal", off_network=True)
    assert ok.needs_allow_cloud
    assert ProviderInfo(**base, kind="cloud", max_privacy="personal").needs_allow_cloud
    assert not ProviderInfo(**base, kind="local", max_privacy="sensitive").needs_allow_cloud


def test_untrusted_memory_limit_needs_allow_cloud() -> None:
    info = ollama(UNTRUSTED, []).info

    assert info.memory_limit_for(allow_cloud=False) is None
    assert info.memory_limit_for(allow_cloud=True) == "personal"


# --- routing -------------------------------------------------------------------------


@pytest.mark.parametrize("privacy", ["public", "personal"])
def test_untrusted_host_needs_allow_cloud(identity: Identity, privacy: PrivacyLevel) -> None:
    seen: list[httpx.Request] = []
    r = router([ollama(UNTRUSTED, seen)], identity)

    with pytest.raises(NoRoute, match="off-network"):
        asyncio.run(r.handle(request(privacy), RouteOptions()))
    assert chats(seen) == []  # nothing sent

    out = asyncio.run(r.handle(request(privacy), RouteOptions(allow_cloud=True)))
    assert out.decision.provider == "ollama" and len(chats(seen)) == 1


def test_untrusted_override_needs_allow_cloud(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    r = router([ollama(UNTRUSTED, seen)], identity)

    with pytest.raises(NoRoute, match="off-network"):
        asyncio.run(r.handle(request(), RouteOptions(override="ollama")))
    assert chats(seen) == []


def test_without_allow_cloud_nothing_else_is_tried(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    cloud = FakeProvider("claude", "cloud")
    r = router([ollama(UNTRUSTED, seen), cloud], identity)

    with pytest.raises(NoRoute):
        asyncio.run(r.handle(request(), RouteOptions()))
    assert chats(seen) == [] and cloud.calls == 0


def test_untrusted_host_is_still_preferred_over_the_cloud(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    cloud = FakeProvider("claude", "cloud")

    out = asyncio.run(
        router([ollama(UNTRUSTED, seen), cloud], identity).handle(
            request(), RouteOptions(allow_cloud=True)
        )
    )
    assert out.decision.provider == "ollama" and cloud.calls == 0

    no_room = asyncio.run(
        router([ollama(UNTRUSTED, seen), cloud], identity, free_gib=0.1).handle(
            request(), RouteOptions(allow_cloud=True)
        )
    )
    assert no_room.decision.provider == "claude"


def test_cloud_failover_to_untrusted_host(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    cloud = FakeProvider("claude", "cloud", error=ProviderUnavailable("claude", "down"))

    out = asyncio.run(
        router([ollama(UNTRUSTED, seen), cloud], identity, free_gib=0.1).handle(
            request(), RouteOptions(allow_cloud=True)
        )
    )

    assert out.decision.provider == "ollama" and out.failed_first is not None


def test_trusted_host_routing_is_unchanged(identity: Identity) -> None:
    seen: list[httpx.Request] = []

    out = asyncio.run(router([ollama(TRUSTED, seen)], identity).handle(request(), RouteOptions()))

    assert out.decision.provider == "ollama" and len(chats(seen)) == 1


def test_opt_in_host_needs_no_allow_cloud(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    r = router([ollama(UNTRUSTED, seen, trust_host=True)], identity)

    out = asyncio.run(r.handle(request("sensitive"), RouteOptions()))

    assert out.decision.provider == "ollama" and len(chats(seen)) == 1


# --- memory --------------------------------------------------------------------------


def system_of(seen: list[httpx.Request]) -> str:
    messages = chats(seen)[-1]["messages"]
    return str(next(m["content"] for m in messages if m["role"] == "system"))


def test_untrusted_host_gets_public_and_personal_memory_with_allow_cloud(
    identity: Identity,
) -> None:
    seen: list[httpx.Request] = []
    src = FakeSource(list(ALL))
    r = router([ollama(UNTRUSTED, seen)], identity, memory=src)

    asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True, include_sensitive=True)))

    system = system_of(seen)
    assert f"{CANARY} public" in system and f"{CANARY} personal" in system
    assert f"{CANARY} sensitive" not in system
    assert [a.limit for a in src.asked] == ["personal"]


def test_opt_in_host_gets_sensitive_memory(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    r = router([ollama(UNTRUSTED, seen, trust_host=True)], identity, memory=FakeSource(list(ALL)))

    asyncio.run(r.handle(request(), RouteOptions(include_sensitive=True)))

    assert f"{CANARY} sensitive" in system_of(seen)


def test_trusted_fake_memory_unchanged(identity: Identity) -> None:
    p = FakeProvider("ollama", "local", memory_limit="sensitive")
    r = router([p], identity, memory=FakeSource(list(ALL)))

    asyncio.run(r.handle(request(), RouteOptions()))

    assert sent_levels(p) == [["personal", "public"]]


# --- tripwire ------------------------------------------------------------------------


def test_tripwire_applies_to_an_untrusted_host(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    r = router([ollama(UNTRUSTED, seen)], identity)

    with pytest.raises(RouteFailed) as caught:
        asyncio.run(r.handle(request(text=f"my key is {FAKE_KEY}"), RouteOptions(allow_cloud=True)))

    assert [a.error_type for a in caught.value.attempts] == [SecretContentRefused.__name__]
    assert chats(seen) == []


@pytest.mark.parametrize("trust", [False, True])
def test_tripwire_does_not_apply_to_a_trusted_host(identity: Identity, trust: bool) -> None:
    seen: list[httpx.Request] = []
    host = UNTRUSTED if trust else TRUSTED
    r = router([ollama(host, seen, trust_host=trust)], identity)

    asyncio.run(r.handle(request(text=f"my key is {FAKE_KEY}"), RouteOptions()))

    assert len(chats(seen)) == 1


def test_tripwire_checks_memory_text_on_an_untrusted_host(identity: Identity) -> None:
    from tests.test_memory_chat import record

    seen: list[httpx.Request] = []
    rec = record("personal", content=f"{CANARY} {FAKE_KEY}")
    r = router([ollama(UNTRUSTED, seen)], identity, memory=FakeSource([rec]))

    with pytest.raises(RouteFailed) as caught:
        asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True)))

    assert [a.error_type for a in caught.value.attempts] == [SecretContentRefused.__name__]
    assert chats(seen) == []


def test_sensitive_request_is_refused_at_routing_even_with_allow_cloud(
    identity: Identity,
) -> None:
    seen: list[httpx.Request] = []
    r = router([ollama(UNTRUSTED, seen)], identity)

    with pytest.raises(NoRoute):
        asyncio.run(r.decide(request("sensitive"), RouteOptions(allow_cloud=True)))
    assert seen == []  # not even an availability check


def test_override_with_allow_cloud_reaches_the_untrusted_host(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    cloud = FakeProvider("claude", "cloud")
    r = router([ollama(UNTRUSTED, seen), cloud], identity, free_gib=0.1)

    out = asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True, override="ollama")))

    assert out.decision.provider == "ollama" and len(chats(seen)) == 1 and cloud.calls == 0


def test_backstop_refuses_memory_for_a_real_off_network_host(identity: Identity) -> None:
    from router.contract import MemoryNotAllowed
    from router.identity_call import call_with_identity
    from tests.test_memory_chat import context_of

    seen: list[httpx.Request] = []
    p = ollama(UNTRUSTED, seen)

    with pytest.raises(MemoryNotAllowed):
        asyncio.run(
            call_with_identity(
                p, request(), identity, secret_check=CHECK, memory=context_of("public")
            )
        )
    assert chats(seen) == []


@pytest.mark.parametrize(
    ("allow_cloud", "expected"),
    [(False, None), (True, "ollama")],
)
def test_choose_route_with_an_off_network_candidate(
    allow_cloud: bool, expected: str | None
) -> None:
    from router.hardware import Headroom
    from router.routing import Candidate, choose_route

    room = Headroom(ok=True, reason="room")
    cands = [Candidate("ollama", "local", True, True, needs_allow_cloud=True)]
    options = RouteOptions(allow_cloud=allow_cloud)

    if expected is None:
        with pytest.raises(NoRoute, match="off-network"):
            choose_route("personal", options, cands, room)
    else:
        assert choose_route("personal", options, cands, room).provider == expected
