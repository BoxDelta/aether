"""llama-swap adapter (ADR-040): contract tests with fakes, no network."""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from core.config import ConfigError, Settings
from core.identity import Identity
from core.log import Redactor, SecretCheck, secrets_from
from router.contract import (
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    PrivacyNotAllowed,
    ProviderBadResponse,
    ProviderTimeout,
    ProviderUnavailable,
    SecretContentRefused,
)
from router.llama_swap import PROVIDER_NAME, LlamaSwapProvider
from router.routing import OFF_NETWORK_MARK, NoRoute, RouteFailed, RouteOptions, Router
from tests.fakes import CHECK, snapshot

URL = "http://127.0.0.1:9292"
MODEL = "qwen-test"
KEY = "llama-swap-fake-key-0123456789"  # gitleaks:allow
CANARY = "SERVER-SIDE-CANARY-4711"
UNTRUSTED = "https://gpu-box.example.org"
FAKE_ANTHROPIC = "sk-ant-api03-FAKE-llama-swap-0123456789"  # gitleaks:allow

Handler = Callable[[httpx.Request], httpx.Response]


def completion(content: Any = "Hello!", finish: str = "stop", **message: Any) -> dict[str, Any]:
    return {
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }


class Recorder:
    def __init__(self, respond: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or self._default

    @staticmethod
    def _default(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "other"}]})
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": []})
        return httpx.Response(200, json=completion())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def chats(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/completions")]


def provider(
    handler: Handler | None = None, url: str = URL, key: str | None = KEY
) -> tuple[LlamaSwapProvider, Recorder]:
    rec = handler if isinstance(handler, Recorder) else Recorder(handler)
    p = LlamaSwapProvider(
        url,
        MODEL,
        api_key=SecretStr(key) if key else None,
        transport=httpx.MockTransport(rec),
    )
    return p, rec


def request(privacy: PrivacyLevel = "personal", text: str = "Hi", **kwargs: Any) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=text),), privacy=privacy, **kwargs)


def generate(p: LlamaSwapProvider, req: ModelRequest) -> ModelResponse:
    return asyncio.run(p.generate(req))


# --- the request -----------------------------------------------------------------


def test_request_goes_to_the_openai_endpoint_with_the_key() -> None:
    p, rec = provider()

    generate(p, request(system="You are Aether.", temperature=0.2, max_tokens=64))

    sent = rec.requests[0]
    assert sent.method == "POST" and str(sent.url) == f"{URL}/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = rec.chats()[0]
    assert body["model"] == MODEL and body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "You are Aether."},
        {"role": "user", "content": "Hi"},
    ]
    assert body["temperature"] == 0.2 and body["max_tokens"] == 64


def test_no_key_means_no_authorization_header() -> None:
    p, rec = provider(key=None)

    generate(p, request())

    assert "authorization" not in rec.requests[0].headers


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"system": "s"}, {"temperature": 0.0}, {"max_tokens": 5}, {"model": "other"}],
)
def test_thinking_is_off_on_every_request(kwargs: dict[str, Any]) -> None:
    p, rec = provider()

    generate(p, request(**kwargs))

    assert rec.chats()[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_request_model_overrides_the_default() -> None:
    p, rec = provider()

    generate(p, request(model="other"))

    assert rec.chats()[0]["model"] == "other"


def test_url_path_query_and_fragment_are_dropped() -> None:
    p, rec = provider(url="http://127.0.0.1:9292/litellm/?x=1#frag")

    generate(p, request())

    assert str(rec.requests[0].url) == f"{URL}/v1/chat/completions"


def test_url_credentials_are_refused_without_echoing_them() -> None:
    with pytest.raises(ConfigError) as caught:
        LlamaSwapProvider("http://user:hunter2-secret@127.0.0.1:9292", MODEL)
    assert "hunter2" not in str(caught.value) and "127.0.0.1" not in str(caught.value)


# --- the reply -------------------------------------------------------------------


def test_reply_is_mapped() -> None:
    p, _ = provider()

    response = generate(p, request())

    assert response.text == "Hello!"
    assert (response.provider, response.kind, response.model) == (PROVIDER_NAME, "local", MODEL)
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (12, 5)


@pytest.mark.parametrize(
    ("reason", "expected"), [("stop", "stop"), ("length", "length"), ("tool_calls", "other")]
)
def test_finish_reason(reason: str, expected: str) -> None:
    p, _ = provider(lambda _: httpx.Response(200, json=completion(finish=reason)))

    assert generate(p, request()).finish_reason == expected


@pytest.mark.parametrize("content", ["", "   \n", None, 7, []])
def test_empty_content_is_a_bad_response(content: Any) -> None:
    p, _ = provider(lambda _: httpx.Response(200, json=completion(content)))

    with pytest.raises(ProviderBadResponse, match="empty content"):
        generate(p, request())


def test_a_reply_with_only_reasoning_is_a_bad_response() -> None:
    body = completion("", reasoning_content="step by step...")
    p, _ = provider(lambda _: httpx.Response(200, json=body))

    with pytest.raises(ProviderBadResponse):
        generate(p, request())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{}]}),
        httpx.Response(200, json=["x"]),
    ],
)
def test_malformed_reply_is_a_bad_response(response: httpx.Response) -> None:
    p, _ = provider(lambda _: response)

    with pytest.raises(ProviderBadResponse):
        generate(p, request())


# --- errors ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (401, ProviderUnavailable, False),
        (403, ProviderUnavailable, False),
        (404, ProviderUnavailable, False),
        (500, ProviderUnavailable, True),
        (503, ProviderUnavailable, True),
        (400, ProviderBadResponse, False),
        (429, ProviderBadResponse, False),
    ],
)
def test_status_mapping_never_copies_server_text(
    status: int, error: type[Exception], retryable: bool
) -> None:
    p, _ = provider(lambda _: httpx.Response(status, text=f"{CANARY} {KEY}"))

    with pytest.raises(error) as caught:
        generate(p, request())

    assert CANARY not in str(caught.value) and KEY not in str(caught.value)
    assert getattr(caught.value, "retryable") is retryable  # noqa: B009


def test_connection_failure_is_unavailable() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {KEY}")

    p, _ = provider(refuse)

    with pytest.raises(ProviderUnavailable) as caught:
        generate(p, request())
    assert KEY not in str(caught.value) and caught.value.retryable


def test_a_read_timeout_is_a_timeout() -> None:
    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    p, _ = provider(slow)

    with pytest.raises(ProviderTimeout):
        generate(p, request())


def test_the_request_deadline_applies() -> None:
    async def hang(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=completion())

    p, _ = provider(hang)  # type: ignore[arg-type]

    with pytest.raises(ProviderTimeout):
        generate(p, request(timeout_s=0.05))


# --- redirects, proxies, one request at a time -----------------------------------


def test_a_redirect_is_not_followed() -> None:
    def redirect(req: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "http://evil.example.org/steal"})

    p, rec = provider(redirect)

    with pytest.raises(ProviderBadResponse):
        generate(p, request(text="private words"))

    assert [r.url.host for r in rec.requests] == ["127.0.0.1"]  # only the checked host


def test_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.example.org:3128")
    # No injected transport: the real client decides whether proxies apply.
    p = LlamaSwapProvider(URL, MODEL)
    control = httpx.AsyncClient()  # trust_env defaults on

    assert control._mounts, "the control client should pick up the proxy variables"
    assert p._client._mounts == {}
    assert p._client.follow_redirects is False


def test_one_request_at_a_time() -> None:
    running = 0
    peak = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return httpx.Response(200, json=completion())

    p, _ = provider(handler)  # type: ignore[arg-type]

    async def run() -> None:
        await asyncio.gather(*(p.generate(request()) for _ in range(4)))

    asyncio.run(run())

    assert peak == 1


# --- availability ----------------------------------------------------------------


def test_available_when_the_model_is_listed_and_never_loads_it() -> None:
    p, rec = provider()

    assert asyncio.run(p.check_available()) is True

    assert [(r.method, r.url.path) for r in rec.requests] == [("GET", "/v1/models")]
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"data": [{"id": "other"}]}),
        httpx.Response(200, json={"data": "nope"}),
        httpx.Response(200, content=b"<html>"),
        httpx.Response(401),
        httpx.Response(500),
        httpx.Response(302, headers={"location": "http://evil.example.org/"}),
    ],
)
def test_not_available(response: httpx.Response) -> None:
    p, rec = provider(lambda _: response)

    assert asyncio.run(p.check_available()) is False
    assert len(rec.requests) == 1


def test_unreachable_is_not_available_and_does_not_raise() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    p, _ = provider(refuse)

    assert asyncio.run(p.check_available()) is False


def test_close_closes_the_client() -> None:
    p, _ = provider()

    asyncio.run(p.aclose())

    assert p._client.is_closed


# --- host trust (ADR-023, ADR-032, ADR-040) --------------------------------------


@pytest.mark.parametrize(
    ("url", "trusted"),
    [
        ("http://127.0.0.1:9292", True),
        ("http://localhost:9292", True),
        ("http://[::1]:9292", True),
        ("http://127.8.9.10:9292", True),
        # Unlike Ollama, private addresses are not trusted (ADR-040):
        ("http://192.168.1.20:9292", False),
        ("http://10.0.0.5:9292", False),
        ("http://169.254.1.1:9292", False),
        (UNTRUSTED, False),
        ("http://gpu-box:9292", False),
        ("http://8.8.8.8:9292", False),
        ("http://100.64.0.1:9292", False),
    ],
)
def test_host_trust_sets_privacy_and_memory_caps(url: str, trusted: bool) -> None:
    info = LlamaSwapProvider(url, MODEL).info

    assert info.kind == "local" and info.off_network is (not trusted)
    assert info.needs_allow_cloud is (not trusted)
    assert info.max_privacy == ("sensitive" if trusted else "personal")
    assert info.memory_limit == ("sensitive" if trusted else "public")  # ADR-040
    assert info.memory_limit_for(allow_cloud=False) == (info.memory_limit if trusted else None)
    assert info.memory_limit_for(allow_cloud=True) == ("sensitive" if trusted else "public")


def test_there_is_no_trust_opt_in() -> None:
    with pytest.raises(TypeError):
        LlamaSwapProvider(UNTRUSTED, MODEL, trust_host=True)  # type: ignore[call-arg]
    assert not any("trust" in name for name in Settings.model_fields if "llama_swap" in name)
    assert LlamaSwapProvider(UNTRUSTED, MODEL).info.off_network  # no env var changes it


def test_an_untrusted_host_refuses_sensitive_before_any_call() -> None:
    p, rec = provider(url=UNTRUSTED)

    with pytest.raises(PrivacyNotAllowed):
        generate(p, request("sensitive"))
    assert rec.requests == []


def test_secret_is_never_sent() -> None:
    p, rec = provider()

    with pytest.raises(PrivacyNotAllowed):
        generate(p, request("secret"))
    assert rec.requests == []


def test_untrusted_host_warning_is_fixed_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aether.router.llama_swap"):
        LlamaSwapProvider(UNTRUSTED + ":8443", MODEL, api_key=SecretStr(KEY))

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert not record.args
    assert "gpu-box" not in record.getMessage() and KEY not in record.getMessage()
    assert "8443" not in record.getMessage()


def test_trusted_host_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="aether.router.llama_swap"):
        LlamaSwapProvider(URL, MODEL)
    assert caplog.records == []


def test_litellm_port_warns_with_fixed_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aether.router.llama_swap"):
        p = LlamaSwapProvider("http://127.0.0.1:4000", MODEL)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "LiteLLM" in message and "4000" not in message and "127.0.0.1" not in message
    assert p.info.off_network is False  # a warning, not a different trust level


# --- through the router ----------------------------------------------------------


def router(p: LlamaSwapProvider, identity: Identity, free_gib: float = 4.0) -> Router:
    return Router([p], identity, secret_check=CHECK, probe=lambda: snapshot(free_gib))


def test_untrusted_host_needs_allow_cloud_and_is_marked(identity: Identity) -> None:
    p, rec = provider(url=UNTRUSTED)
    r = router(p, identity)

    with pytest.raises(NoRoute, match="off-network"):
        asyncio.run(r.handle(request(), RouteOptions()))
    assert rec.chats() == []

    out = asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True)))
    assert out.decision.provider == PROVIDER_NAME
    assert out.decision.reason.endswith(OFF_NETWORK_MARK)
    assert len(rec.chats()) == 1


def test_untrusted_host_gets_the_content_tripwire(identity: Identity) -> None:
    p, rec = provider(url=UNTRUSTED)
    r = router(p, identity)

    with pytest.raises(RouteFailed) as caught:
        asyncio.run(
            r.handle(request(text=f"my key is {FAKE_ANTHROPIC}"), RouteOptions(allow_cloud=True))
        )

    assert [a.error_type for a in caught.value.attempts] == [SecretContentRefused.__name__]
    assert rec.chats() == []


def test_trusted_host_is_local_without_allow_cloud(identity: Identity) -> None:
    p, rec = provider()

    out = asyncio.run(router(p, identity).handle(request("sensitive"), RouteOptions()))

    assert out.decision.provider == PROVIDER_NAME and OFF_NETWORK_MARK not in out.decision.reason
    assert len(rec.chats()) == 1


def test_the_identity_goes_with_every_call(identity: Identity) -> None:
    p, rec = provider()

    asyncio.run(router(p, identity).handle(request(), RouteOptions()))

    messages = rec.chats()[0]["messages"]
    assert messages[0]["role"] == "system" and identity.name in messages[0]["content"]


# --- the key is a secret everywhere ----------------------------------------------


def test_the_key_is_redacted_and_refused_by_value() -> None:
    settings = Settings(_env_file=None, llama_swap_api_key=SecretStr(KEY))

    assert KEY in secrets_from(settings)
    assert KEY not in Redactor(secrets_from(settings))(f"sent {KEY} to it")
    assert SecretCheck.from_settings(settings)(f"the key is {KEY}")
    assert KEY not in repr(settings)


def test_settings_defaults_and_empty_values() -> None:
    s = Settings(_env_file=None)
    assert str(s.llama_swap_url) == "http://127.0.0.1:9292/"
    assert s.llama_swap_model is None and s.llama_swap_api_key is None
    assert s.aether_ollama_enabled is False

    blank = Settings(
        _env_file=None,
        llama_swap_model=" ",
        llama_swap_api_key=SecretStr(""),
        aether_ollama_enabled="",  # type: ignore[arg-type]
    )
    assert blank.llama_swap_model is None and blank.llama_swap_api_key is None
    assert blank.aether_ollama_enabled is False


def test_from_settings_needs_a_model() -> None:
    with pytest.raises(ConfigError):
        LlamaSwapProvider.from_settings(Settings(_env_file=None))
    p = LlamaSwapProvider.from_settings(Settings(_env_file=None, llama_swap_model=MODEL))
    assert p.info.default_model == MODEL


def test_a_short_key_is_refused_without_echo() -> None:
    with pytest.raises(ConfigError) as caught:
        LlamaSwapProvider(URL, MODEL, api_key=SecretStr("abc123"))
    assert "abc123" not in str(caught.value)


def test_key_over_plain_http_off_network_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aether.router.llama_swap"):
        LlamaSwapProvider("http://gpu-box.example.org:9292", MODEL, api_key=SecretStr(KEY))
    assert any("plain text" in r.getMessage() for r in caplog.records)
    assert all(KEY not in r.getMessage() for r in caplog.records)


def test_a_private_lan_host_needs_allow_cloud_and_gets_public_memory(identity: Identity) -> None:
    p, rec = provider(url="http://192.168.1.20:9292")
    r = router(p, identity)

    with pytest.raises(NoRoute, match="off-network"):
        asyncio.run(r.handle(request(), RouteOptions()))
    assert rec.requests == []  # not even /running or /v1/models
    out = asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True)))
    assert out.decision.reason.endswith(OFF_NETWORK_MARK)


def _probe(free_mib: int) -> Any:
    from router.hardware import MIB, GpuState, HardwareSnapshot

    def probe() -> HardwareSnapshot:
        gpu = GpuState("nvml", 8192 * MIB, free_mib * MIB, 5)
        return HardwareSnapshot(gpu=gpu, ram_available_bytes=16 * 1024**3)

    return probe


def running(state: str | None = "ready", model: str = MODEL) -> Handler:
    """llama-swap that reports ``model`` in ``state`` on /running (None: nothing)."""

    def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            entries = [] if state is None else [{"model": model, "state": state}]
            return httpx.Response(200, json={"running": entries})
        return Recorder._default(req)

    return respond


def _route(
    identity: Identity, handler: Handler, free_mib: int, privacy: PrivacyLevel = "personal"
) -> str:
    from tests.fakes import FakeProvider

    p, _ = provider(handler)
    r = Router(
        [p, FakeProvider("claude", "cloud")],
        identity,
        secret_check=CHECK,
        probe=_probe(free_mib),
    )
    out = asyncio.run(r.handle(request(privacy), RouteOptions(allow_cloud=True)))
    return out.decision.provider


@pytest.mark.parametrize("privacy", ["public", "personal"])
def test_loaded_model_with_low_free_vram_still_routes_locally(
    identity: Identity, privacy: PrivacyLevel
) -> None:
    # 1,131 MiB free is below the reserve, as with Qwen loaded (README §2.3).
    assert _route(identity, running("ready"), 1131, privacy) == PROVIDER_NAME


@pytest.mark.parametrize("free_mib", [1131, 3000, 5200 + 1535])
def test_cold_model_without_enough_vram_has_no_local_route(
    identity: Identity, free_mib: int
) -> None:
    assert _route(identity, running(None), free_mib) == "claude"


@pytest.mark.parametrize("free_mib", [5200 + 1536, 7105])
def test_cold_model_with_enough_vram_routes_locally(identity: Identity, free_mib: int) -> None:
    assert _route(identity, running(None), free_mib) == PROVIDER_NAME


def test_cold_reason_names_the_cold_start(identity: Identity) -> None:
    from tests.fakes import FakeProvider

    p, _ = provider(running(None))
    r = Router(
        [p, FakeProvider("claude", "cloud")], identity, secret_check=CHECK, probe=_probe(3000)
    )

    out = asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True)))

    assert "model not loaded" in out.decision.reason


def test_nvml_cannot_make_a_cold_model_look_loaded(identity: Identity) -> None:
    # Whatever the GPU says, only /running decides loaded vs cold.
    assert _route(identity, running(None), 1131) == "claude"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"running": [{"model": MODEL, "state": "starting"}]}),
        httpx.Response(200, json={"running": [{"model": "other", "state": "ready"}]}),
        httpx.Response(200, json={"running": "nope"}),
        httpx.Response(200, json={}),
        httpx.Response(200, content=b"<html>"),
        httpx.Response(401),
        httpx.Response(500),
        httpx.Response(307, headers={"location": "http://evil.example.org/running"}),
    ],
)
def test_anything_but_a_ready_model_is_cold(response: httpx.Response) -> None:
    p, rec = provider(lambda _: response)

    assert asyncio.run(p.is_loaded()) is False
    assert [r.url.host for r in rec.requests] == ["127.0.0.1"]


def test_an_unreachable_or_slow_running_endpoint_is_cold() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async def hang(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"running": [{"model": MODEL, "state": "ready"}]})

    p, _ = provider(refuse)
    assert asyncio.run(p.is_loaded()) is False
    slow, _ = provider(hang)  # type: ignore[arg-type]
    started = time.monotonic()
    assert asyncio.run(slow.is_loaded()) is False
    assert time.monotonic() - started < 3


def test_the_running_check_uses_the_same_client_rules() -> None:
    p, rec = provider(running("ready"))

    assert asyncio.run(p.is_loaded()) is True

    assert [(r.method, r.url.path) for r in rec.requests] == [("GET", "/running")]
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert p._client.follow_redirects is False


def test_the_running_check_never_generates_or_loads() -> None:
    p, rec = provider(running("ready"))

    asyncio.run(p.is_loaded())
    asyncio.run(p.check_available())

    assert rec.chats() == []
    assert all(r.method == "GET" for r in rec.requests)


def test_model_vram_setting() -> None:
    assert Settings(_env_file=None).llama_swap_model_vram_mib == 5200
    assert Settings(_env_file=None, llama_swap_model_vram_mib="").llama_swap_model_vram_mib == 5200  # type: ignore[arg-type]
    p = LlamaSwapProvider.from_settings(
        Settings(_env_file=None, llama_swap_model=MODEL, llama_swap_model_vram_mib=6000)
    )
    assert p.vram_bytes == 6000 * 1024 * 1024


def test_an_untrusted_host_gets_public_memory_only(identity: Identity) -> None:
    from tests.test_memory_chat import FakeSource, record, sent_levels  # noqa: F401

    p, rec = provider(url="http://192.168.1.20:9292")
    source = FakeSource([record("public"), record("personal")])
    r = Router([p], identity, secret_check=CHECK, probe=lambda: snapshot(4.0), memory=source)

    asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True)))

    assert [a.limit for a in source.asked] == ["public"]
    system = rec.chats()[0]["messages"][0]["content"]
    assert "public fact" in system and "personal fact" not in system


def test_no_request_reaches_an_untrusted_host_for_a_cloud_override(identity: Identity) -> None:
    from tests.fakes import FakeProvider

    p, rec = provider(url="http://192.168.1.20:9292")
    r = Router(
        [p, FakeProvider("claude", "cloud")],
        identity,
        secret_check=CHECK,
        probe=_probe(7000),
    )

    out = asyncio.run(r.handle(request(), RouteOptions(allow_cloud=True, override="claude")))

    assert out.decision.provider == "claude" and rec.requests == []
