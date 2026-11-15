import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import AnyHttpUrl

from core.config import Settings
from router.contract import (
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyNotAllowed,
    ProviderBadResponse,
    ProviderTimeout,
    ProviderUnavailable,
)
from router.ollama import OllamaProvider

HOST = "http://127.0.0.1:11434"
MODEL = "llama3.1:8b"

Handler = Callable[[httpx.Request], httpx.Response]


def chat_body(content: str | None = "Hello!", done_reason: str = "stop") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    return {
        "model": MODEL,
        "created_at": "2026-11-14T08:00:00Z",
        "message": message,
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 12,
        "eval_count": 5,
    }


class Recorder:
    """MockTransport handler that records requests and returns a canned response."""

    def __init__(self, respond: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def json(self, index: int = -1) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[index].content)
        return body


def provider(handler: Handler) -> OllamaProvider:
    return OllamaProvider(HOST, MODEL, transport=httpx.MockTransport(handler))


def request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content="Hi"),), **kwargs)


def generate(p: OllamaProvider, req: ModelRequest) -> ModelResponse:
    return asyncio.run(p.generate(req))


# --- success ---------------------------------------------------------------------


def test_successful_chat_maps_response() -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=chat_body()))

    response = generate(provider(rec), request())

    assert response.text == "Hello!"
    assert response.provider == "ollama"
    assert response.kind == "local"
    assert response.model == MODEL
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (12, 5)
    assert rec.requests[0].url == f"{HOST}/api/chat"


def test_request_translation_with_system_and_options() -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=chat_body()))
    req = ModelRequest(
        messages=(
            Message(role="user", content="Q1"),
            Message(role="assistant", content="A1"),
            Message(role="user", content="Q2"),
        ),
        system="You are Aether.",
        model="qwen2.5:7b",
        temperature=0.2,
        max_tokens=64,
    )

    generate(provider(rec), req)

    body = rec.json()
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "You are Aether."},
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]
    assert body["options"]["temperature"] == 0.2
    assert body["options"]["num_predict"] == 64


def test_defaults_omit_system_and_use_default_model() -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=chat_body()))

    generate(provider(rec), request())

    body = rec.json()
    assert body["model"] == MODEL
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert not body.get("options")


@pytest.mark.parametrize(("done_reason", "expected"), [("length", "length"), ("unload", "other")])
def test_finish_reason_mapping(done_reason: str, expected: str) -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=chat_body(done_reason=done_reason)))

    assert generate(provider(rec), request()).finish_reason == expected


def test_trailing_slash_host_from_settings_works() -> None:
    settings = Settings(_env_file=None, ollama_host=AnyHttpUrl("http://127.0.0.1:11434/"))
    p = OllamaProvider.from_settings(settings)

    assert p.info.default_model == "llama3.1:8b"
    assert p.info.max_privacy == "sensitive"
    assert p._client._client.base_url == httpx.URL(HOST)


# --- failures --------------------------------------------------------------------


def raises(exc: Exception) -> Handler:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def test_connection_refused_is_unavailable_and_retryable() -> None:
    p = provider(raises(httpx.ConnectError("Connection refused")))

    with pytest.raises(ProviderUnavailable, match="cannot connect") as caught:
        generate(p, request())

    assert caught.value.retryable is True


def test_http_timeout_is_provider_timeout() -> None:
    p = provider(raises(httpx.ReadTimeout("timed out")))

    with pytest.raises(ProviderTimeout) as caught:
        generate(p, request())

    assert caught.value.retryable is True


def test_other_transport_error_is_unavailable() -> None:
    p = provider(raises(httpx.RemoteProtocolError("peer closed")))

    with pytest.raises(ProviderUnavailable, match="RemoteProtocolError"):
        generate(p, request())


def test_slow_model_hits_request_deadline() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=chat_body())

    p = OllamaProvider(HOST, MODEL, transport=httpx.MockTransport(slow))

    with pytest.raises(ProviderTimeout):
        generate(p, request(timeout_s=0.05))


def test_model_not_found_is_unavailable_not_retryable() -> None:
    rec = Recorder(lambda _: httpx.Response(404, json={"error": "model 'x' not found"}))

    with pytest.raises(ProviderUnavailable, match="ollama pull") as caught:
        generate(provider(rec), request())

    assert caught.value.retryable is False


def test_server_error_is_unavailable_and_retryable() -> None:
    rec = Recorder(lambda _: httpx.Response(500, json={"error": "CUDA out of memory"}))

    with pytest.raises(ProviderUnavailable, match="HTTP 500") as caught:
        generate(provider(rec), request())

    assert caught.value.retryable is True


def test_client_error_is_bad_response() -> None:
    rec = Recorder(lambda _: httpx.Response(400, json={"error": "bad"}))

    with pytest.raises(ProviderBadResponse, match="HTTP 400"):
        generate(provider(rec), request())


def test_ollama_error_text_is_not_copied_into_error_message() -> None:
    # Ollama error bodies can echo request content; only safe parts are kept.
    echoed = "prompt was: my private diary entry"
    rec = Recorder(lambda _: httpx.Response(500, json={"error": echoed}))

    with pytest.raises(ProviderUnavailable) as caught:
        generate(provider(rec), request())

    assert echoed not in str(caught.value)
    assert echoed not in caught.value.message


@pytest.mark.parametrize(
    "body",
    [
        chat_body(content=None),
        {"unexpected": "shape"},
    ],
)
def test_malformed_response_is_bad_response(body: dict[str, Any]) -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=body))

    with pytest.raises(ProviderBadResponse):
        generate(provider(rec), request())


def test_invalid_json_is_bad_response() -> None:
    rec = Recorder(lambda _: httpx.Response(200, content=b"<html>proxy error</html>"))

    with pytest.raises(ProviderBadResponse):
        generate(provider(rec), request())


def test_secret_request_never_reaches_ollama() -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=chat_body()))

    with pytest.raises(PrivacyNotAllowed):
        generate(provider(rec), request(privacy="secret"))

    assert rec.requests == []


def test_sensitive_request_is_allowed_locally() -> None:
    rec = Recorder(lambda _: httpx.Response(200, json=chat_body()))

    generate(provider(rec), request(privacy="sensitive"))

    assert len(rec.requests) == 1


# --- availability ----------------------------------------------------------------


def tags(*names: str) -> Handler:
    return lambda _: httpx.Response(200, json={"models": [{"model": n, "name": n} for n in names]})


@pytest.mark.parametrize(
    ("default_model", "pulled", "expected"),
    [
        (MODEL, (MODEL, "other:1b"), True),
        (MODEL, ("other:1b",), False),
        ("mistral", ("mistral:latest",), True),
        ("mistral", ("mistral:7b",), False),
    ],
)
def test_check_available_requires_default_model(
    default_model: str, pulled: tuple[str, ...], expected: bool
) -> None:
    p = OllamaProvider(HOST, default_model, transport=httpx.MockTransport(tags(*pulled)))

    assert asyncio.run(p.check_available()) is expected


@pytest.mark.parametrize(
    "handler",
    [
        raises(httpx.ConnectError("refused")),
        lambda _: httpx.Response(500, text="boom"),
        lambda _: httpx.Response(200, content=b"not json"),
    ],
)
def test_check_available_never_raises(handler: Handler) -> None:
    assert asyncio.run(provider(handler).check_available()) is False
