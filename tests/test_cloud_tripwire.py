"""The content tripwire for cloud sends (#8, docs/05 §7.1).

A cloud send whose text looks like a secret (the log redactor's patterns or a
configured secret value) is refused in ``call_with_identity`` before anything is
sent: ``SecretContentRefused``, not retryable, so no failover; ``/chat`` answers
422. Local sends are not checked. Every secret here is an invented fake from
``tests/fake_secrets.py``; responses, logs and events are checked not to hold it.
"""

import asyncio
import json
from typing import Any

import pytest
from pydantic import SecretStr

from core.config import Settings
from core.identity import Identity
from core.log import SecretCheck, configure_logging
from router.contract import Message, ModelRequest, ProviderUnavailable, SecretContentRefused
from router.identity_call import call_with_identity
from router.routing import RouteFailed, RouteOptions, Router
from tests.chat_helpers import TOKEN, Setup
from tests.fake_secrets import NORMAL_TEXTS, FakeSecret, by_family, configured_value
from tests.fakes import FakeProvider, snapshot

# The configured API token (32 characters, the minimum), also the fakes' configured value.
CONFIGURED = configured_value() + "0"
FAKES = by_family(CONFIGURED)
SETTINGS = Settings(_env_file=None, aether_api_token=SecretStr(CONFIGURED))


def check() -> SecretCheck:
    return SecretCheck.from_settings(SETTINGS)


def request(text: str, *, system: str | None = None) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),), system=system)


def send(provider: FakeProvider, req: ModelRequest, identity: Identity) -> Any:
    return asyncio.run(call_with_identity(provider, req, identity, secret_check=check()))


# --- call_with_identity: the one call path ------------------------------------------


@pytest.mark.parametrize("fake", FAKES, ids=lambda f: f.family)
def test_a_cloud_send_that_looks_secret_is_refused_unsent(
    fake: FakeSecret, identity: Identity, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(SETTINGS)
    cloud = FakeProvider("claude", "cloud")
    with pytest.raises(SecretContentRefused) as info:
        send(cloud, request(fake.text), identity)

    assert cloud.calls == 0 and cloud.requests == []
    exc = info.value
    assert exc.retryable is False and exc.provider == "claude"
    assert exc.message == SecretContentRefused.MESSAGE
    assert fake.core not in str(exc)
    out = capsys.readouterr().out
    [line] = [json.loads(x) for x in out.splitlines() if "Model call failed" in x]
    assert line["error_type"] == "SecretContentRefused" and line["outcome"] == "error"
    assert fake.core not in out


@pytest.mark.parametrize("fake", FAKES[:2], ids=lambda f: f.family)
def test_the_system_prompt_and_every_message_are_checked(
    fake: FakeSecret, identity: Identity
) -> None:
    cloud = FakeProvider("claude", "cloud")
    with pytest.raises(SecretContentRefused):
        send(cloud, request("hello", system=f"task notes: {fake.text}"), identity)
    many = ModelRequest(
        messages=(
            Message(role="user", content="first question"),
            Message(role="assistant", content="first answer"),
            Message(role="user", content=fake.text),
        )
    )
    with pytest.raises(SecretContentRefused):
        send(cloud, many, identity)
    assert cloud.calls == 0


@pytest.mark.parametrize("text", NORMAL_TEXTS)
def test_ordinary_text_still_goes_to_the_cloud(text: str, identity: Identity) -> None:
    cloud = FakeProvider("claude", "cloud")
    send(cloud, request(text), identity)
    assert cloud.calls == 1


def test_local_sends_are_not_checked(identity: Identity) -> None:
    # The text stays on the owner's machine; the privacy level rules still apply.
    local = FakeProvider("ollama", "local")
    send(local, request(FAKES[0].text), identity)
    assert local.calls == 1


@pytest.mark.parametrize("bad", [None, lambda text: False, object()])
def test_a_secret_check_is_required(bad: Any, identity: Identity) -> None:
    cloud = FakeProvider("claude", "cloud")
    with pytest.raises(TypeError, match="SecretCheck"):
        asyncio.run(call_with_identity(cloud, request("hi"), identity, secret_check=bad))
    with pytest.raises(TypeError, match="SecretCheck"):
        Router([cloud], identity, secret_check=bad)
    assert cloud.calls == 0


def test_core_router_checks_configured_values(identity: Identity) -> None:
    router = Router.from_settings(SETTINGS, identity)
    assert router._secret_check(f"note {CONFIGURED}") is True
    assert router._secret_check("a note about tea") is False


# --- routing: no failover, and failover can't bypass it ------------------------------


def _router(identity: Identity, local: FakeProvider, cloud: FakeProvider, free: float) -> Router:
    return Router([local, cloud], identity, secret_check=check(), probe=lambda: snapshot(free))


def test_a_refused_cloud_send_does_not_fail_over(identity: Identity) -> None:
    # No local headroom, so cloud is chosen; the refusal is final.
    local, cloud = FakeProvider("ollama", "local"), FakeProvider("claude", "cloud")
    router = _router(identity, local, cloud, free=0.0)
    with pytest.raises(RouteFailed) as info:
        asyncio.run(router.handle(request(FAKES[0].text), RouteOptions(allow_cloud=True)))
    assert [(a.provider, a.error_type) for a in info.value.attempts] == [
        ("claude", "SecretContentRefused")
    ]
    assert local.calls == 0 and cloud.calls == 0


def test_failover_to_the_cloud_is_refused_too(identity: Identity) -> None:
    local = FakeProvider("ollama", "local", error=ProviderUnavailable("ollama", "down"))
    cloud = FakeProvider("claude", "cloud")
    router = _router(identity, local, cloud, free=4.0)
    with pytest.raises(RouteFailed) as info:
        asyncio.run(router.handle(request(FAKES[1].text), RouteOptions(allow_cloud=True)))
    assert [(a.provider, a.error_type) for a in info.value.attempts] == [
        ("ollama", "ProviderUnavailable"),
        ("claude", "SecretContentRefused"),
    ]
    assert local.calls == 1 and cloud.calls == 0


# --- /chat end to end -----------------------------------------------------------------


def _chat_refused(s: Setup, text: str, **options: Any) -> dict[str, Any]:
    r = s.client.post("/chat", json={"messages": [{"role": "user", "content": text}], **options})
    assert r.status_code == 422, r.text
    body: dict[str, Any] = r.json()
    assert body["error"]["type"] == "SecretContentRefused"
    assert body["error"]["message"] == SecretContentRefused.MESSAGE
    assert s.cloud.calls == 0
    return body


@pytest.mark.parametrize("fake", by_family(TOKEN), ids=lambda f: f.family)
def test_chat_refuses_a_cloud_send_with_422(fake: FakeSecret) -> None:
    # by_family(TOKEN): the configured value is Core's own API token.
    s = Setup(free_gib=0.0)
    body = _chat_refused(s, fake.text, allow_cloud=True)
    assert [a["error_type"] for a in body["error"]["attempts"]] == ["SecretContentRefused"]
    everything = json.dumps(body) + s.log.getvalue() + json.dumps(s.rows, default=str)
    assert fake.core not in everything
    [failed] = [e for e in s.rows if e["event_type"] == "request.failed"]
    assert failed["attempts"][0]["error_type"] == "SecretContentRefused"


def test_chat_override_to_the_cloud_is_refused() -> None:
    s = Setup()
    _chat_refused(s, FAKES[0].text, allow_cloud=True, override="claude")
    assert s.local.calls == 0


def test_chat_failover_to_the_cloud_is_refused() -> None:
    s = Setup(local_error=ProviderUnavailable("ollama", "down"))
    body = _chat_refused(s, FAKES[0].text, allow_cloud=True)
    assert [a["provider"] for a in body["error"]["attempts"]] == ["ollama", "claude"]


def test_chat_sends_the_same_text_locally() -> None:
    s = Setup()
    r = s.client.post("/chat", json={"messages": [{"role": "user", "content": FAKES[0].text}]})
    assert r.status_code == 200
    assert s.local.calls == 1 and s.cloud.calls == 0
