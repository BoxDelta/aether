import asyncio
import io
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient
from pydantic import SecretStr

from core.config import DEFAULT_CLOUD_MAX_OUTPUT_TOKENS, Settings
from core.log import configure_logging
from router.claude import ANTHROPIC_API_BASE_URL, FALLBACK_BETA, ClaudeProvider
from router.contract import (
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyNotAllowed,
    ProviderBadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)

# Fake key for tests only; never a real credential.
FAKE_KEY = "sk-ant-api03-FAKE-test-key-0123456789abcdef"  # gitleaks:allow
MODEL = "claude-opus-5-5"

Handler = Callable[[httpx2.Request], httpx2.Response]


def message_body(
    *,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    model: str = MODEL,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": "Hello!"}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {"input_tokens": 21, "output_tokens": 4},
    }


def error_body(status: int, message: str) -> httpx2.Response:
    kinds = {
        400: "invalid_request_error",
        413: "request_too_large",
        422: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        408: "timeout_error",
        429: "rate_limit_error",
        500: "api_error",
        529: "overloaded_error",
    }
    return httpx2.Response(
        status,
        json={
            "type": "error",
            "error": {"type": kinds.get(status, "api_error"), "message": message},
        },
        headers={"request-id": "req_test123"},
    )


class Recorder:
    def __init__(self, respond: Handler) -> None:
        self.requests: list[httpx2.Request] = []
        self._respond = respond

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._respond(request)

    def json(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body


def provider(
    handler: Handler, key: str | None = FAKE_KEY, max_output_tokens: int | None = None
) -> ClaudeProvider:
    extra = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
    return ClaudeProvider(
        SecretStr(key) if key is not None else None,
        MODEL,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,  # tests: no SDK backoff
        **extra,
    )


def request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content="Hi"),), **kwargs)


def generate(p: ClaudeProvider, req: ModelRequest | None = None) -> ModelResponse:
    return asyncio.run(p.generate(req or request()))


def ok(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=message_body())


# --- success and request translation ---------------------------------------------


def test_successful_message_maps_response() -> None:
    rec = Recorder(ok)

    response = generate(provider(rec))

    assert response.text == "Hello!"
    assert response.provider == "claude"
    assert response.kind == "cloud"
    assert response.model == MODEL
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (21, 4)
    assert rec.requests[0].url.path == "/v1/messages"


def test_request_translation() -> None:
    rec = Recorder(ok)
    req = ModelRequest(
        messages=(
            Message(role="user", content="Q1"),
            Message(role="assistant", content="A1"),
            Message(role="user", content="Q2"),
        ),
        system="You are Aether.",
        model="claude-sonnet-5-5",
        max_tokens=512,
        temperature=0.3,
    )

    generate(provider(rec), req)

    body = rec.json()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 512
    assert body["system"] == "You are Aether."
    assert body["messages"] == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]
    assert "temperature" not in body  # rejected by the API version the SDK uses


def test_defaults_and_refusal_fallback_opt_in() -> None:
    rec = Recorder(ok)

    generate(provider(rec))

    body = rec.json()
    headers = rec.requests[-1].headers
    assert body["model"] == MODEL
    assert body["max_tokens"] == DEFAULT_CLOUD_MAX_OUTPUT_TOKENS == 4096
    assert "system" not in body
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in headers["anthropic-beta"]


def test_api_key_is_sent_only_in_its_header() -> None:
    rec = Recorder(ok)

    generate(provider(rec))

    sent = rec.requests[-1]
    # anthropic SDK 2.x sends the key in the Authorization header.
    carrying_key = [name for name, value in sent.headers.items() if FAKE_KEY in value]
    assert carrying_key == ["authorization"]
    assert FAKE_KEY not in sent.content.decode()
    assert FAKE_KEY not in str(sent.url)


def test_environment_cannot_redirect_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # #9: a stray ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN must not change where the
    # key and prompts go, or which credential is sent.
    env_credential = "env-auth-token-should-never-be-sent"  # gitleaks:allow
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.example.net")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", env_credential)
    rec = Recorder(ok)

    p = provider(rec)
    generate(p)

    assert ANTHROPIC_API_BASE_URL == "https://api.anthropic.com"
    assert p._client is not None
    assert str(p._client.base_url).rstrip("/") == ANTHROPIC_API_BASE_URL
    assert p._client.auth_token is None
    sent = rec.requests[-1]
    assert sent.url.scheme == "https"
    assert sent.url.host == "api.anthropic.com"
    assert sent.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert all(env_credential not in value for value in sent.headers.values())
    assert "attacker.example.net" not in str(sent.url)


def test_only_text_blocks_become_text() -> None:
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Part one. "},
        {"type": "text", "text": "Part two."},
    ]
    rec = Recorder(lambda _: httpx2.Response(200, json=message_body(content=content)))

    assert generate(provider(rec)).text == "Part one. Part two."


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("pause_turn", "other"),
    ],
)
def test_stop_reason_mapping(stop_reason: str, expected: str) -> None:
    rec = Recorder(lambda _: httpx2.Response(200, json=message_body(stop_reason=stop_reason)))

    assert generate(provider(rec)).finish_reason == expected


def test_refusal_has_no_text_and_logs_only_category(caplog: pytest.LogCaptureFixture) -> None:
    body = message_body(
        content=[{"type": "text", "text": "partial output before decline"}],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    )
    rec = Recorder(lambda _: httpx2.Response(200, json=body))
    caplog.set_level(logging.INFO, logger="aether.router.claude")

    response = generate(provider(rec))

    assert response.finish_reason == "refusal"
    assert response.text == ""
    [record] = [r for r in caplog.records if r.getMessage() == "Claude declined the request"]
    assert record.__dict__["refusal_category"] == "cyber"


def test_fallback_model_is_reported() -> None:
    rec = Recorder(lambda _: httpx2.Response(200, json=message_body(model="claude-opus-4-8")))

    assert generate(provider(rec)).model == "claude-opus-4-8"


# --- missing key -------------------------------------------------------------------


def test_missing_key_is_unavailable_and_makes_no_request() -> None:
    rec = Recorder(ok)
    p = provider(rec, key=None)

    assert asyncio.run(p.check_available()) is False
    with pytest.raises(ProviderUnavailable, match="no API key") as caught:
        generate(p)

    assert caught.value.retryable is False
    assert rec.requests == []


def test_configured_key_is_available() -> None:
    assert asyncio.run(provider(ok).check_available()) is True


def test_from_settings_without_key() -> None:
    p = ClaudeProvider.from_settings(Settings(_env_file=None))

    assert p.info.default_model == "claude-opus-5-5"
    assert p.info.max_privacy == "personal"
    assert asyncio.run(p.check_available()) is False


# --- error mapping -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "retryable", "fragment"),
    [
        (400, ProviderBadResponse, False, "request rejected"),
        (413, ProviderBadResponse, False, "request rejected"),  # #121
        (422, ProviderBadResponse, False, "request rejected"),
        (401, ProviderUnavailable, False, "authentication failed"),
        (403, ProviderUnavailable, False, "authentication failed"),
        (404, ProviderUnavailable, False, "model not found"),
        (408, ProviderTimeout, True, "timed out"),
        (429, ProviderUnavailable, True, "rate limited"),
        (500, ProviderUnavailable, True, "server error"),
        (529, ProviderUnavailable, True, "overloaded"),
    ],
)
def test_http_error_mapping(
    status: int, error_type: type[ProviderError], retryable: bool, fragment: str
) -> None:
    rec = Recorder(lambda _: error_body(status, "something went wrong"))

    with pytest.raises(error_type) as caught:
        generate(provider(rec))

    assert caught.value.retryable is retryable
    assert fragment in caught.value.message
    assert f"HTTP {status}" in caught.value.message
    assert "req_test123" in caught.value.message


def raises(exc: Exception) -> Handler:
    def handler(_: httpx2.Request) -> httpx2.Response:
        raise exc

    return handler


def test_connection_error_is_unavailable() -> None:
    with pytest.raises(ProviderUnavailable, match="cannot connect") as caught:
        generate(provider(raises(httpx2.ConnectError("refused"))))

    assert caught.value.retryable is True


def test_sdk_timeout_is_provider_timeout() -> None:
    with pytest.raises(ProviderTimeout):
        generate(provider(raises(httpx2.ReadTimeout("slow"))))


def test_request_deadline_is_enforced() -> None:
    async def slow(_: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=message_body())

    p = ClaudeProvider(
        SecretStr(FAKE_KEY),
        MODEL,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(slow)),
        max_retries=0,
    )

    with pytest.raises(ProviderTimeout):
        generate(p, request(timeout_s=0.05))


@pytest.mark.parametrize(
    "respond",
    [
        lambda _: httpx2.Response(200, content=b"<html>gateway</html>"),
        lambda _: httpx2.Response(200, json={"unexpected": "shape"}),
    ],
)
def test_malformed_success_is_bad_response(respond: Handler) -> None:
    with pytest.raises(ProviderBadResponse):
        generate(provider(respond))


# --- the API key never appears in errors ---------------------------------------------


def _status_echo(status: int, text: str) -> Handler:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return error_body(status, text)

    return handler


def _key_echo_handlers() -> list[Handler]:
    echo = f"invalid x-api-key: {FAKE_KEY}"
    handlers = [_status_echo(s, echo) for s in (400, 401, 403, 404, 408, 429, 500, 529)]
    handlers += [
        raises(httpx2.ConnectError(f"refused while sending {FAKE_KEY}")),
        raises(httpx2.ReadTimeout(f"timeout for {FAKE_KEY}")),
        lambda _: httpx2.Response(200, content=f"<html>{FAKE_KEY}</html>".encode()),
    ]
    return handlers


@pytest.mark.parametrize("handler", _key_echo_handlers())
def test_error_messages_never_contain_the_api_key(handler: Handler) -> None:
    with pytest.raises(ProviderError) as caught:
        generate(provider(handler))

    error = caught.value
    assert FAKE_KEY not in error.message
    assert FAKE_KEY not in str(error)
    assert FAKE_KEY not in repr(error)
    assert FAKE_KEY not in repr(error.args)


def test_logged_provider_error_with_cause_is_redacted() -> None:
    # The chained SDK exception may contain the key; it must not survive logging.
    out = io.StringIO()
    configure_logging(Settings(_env_file=None, anthropic_api_key=SecretStr(FAKE_KEY)), stream=out)
    rec = Recorder(lambda _: error_body(401, f"invalid x-api-key: {FAKE_KEY}"))

    try:
        generate(provider(rec))
    except ProviderError:
        logging.getLogger("aether.test").exception("provider call failed")

    assert "provider call failed" in out.getvalue()
    assert FAKE_KEY not in out.getvalue()


def test_secret_or_sensitive_request_never_reaches_claude() -> None:
    rec = Recorder(ok)
    p = provider(rec)

    for privacy in ("sensitive", "secret"):
        with pytest.raises(PrivacyNotAllowed):
            generate(p, request(privacy=privacy))

    assert rec.requests == []


# --- output token cap (#77) ---------------------------------------------------------


def usage_body(
    *, content: list[dict[str, Any]], stop_reason: str, output_tokens: int
) -> dict[str, Any]:
    body = message_body(content=content, stop_reason=stop_reason)
    body["usage"] = {"input_tokens": 21, "output_tokens": output_tokens}
    return body


def test_cap_is_sent_when_request_has_no_limit() -> None:
    rec = Recorder(ok)

    generate(provider(rec, max_output_tokens=1024))

    assert rec.json()["max_tokens"] == 1024


def test_request_limit_below_cap_is_sent() -> None:
    rec = Recorder(ok)

    generate(provider(rec, max_output_tokens=1024), request(max_tokens=200))

    assert rec.json()["max_tokens"] == 200


def test_request_limit_above_cap_is_clamped() -> None:
    # Backstop: Core rejects this at /chat, but other callers are clamped here.
    rec = Recorder(ok)

    generate(provider(rec, max_output_tokens=1024), request(max_tokens=50_000))

    assert rec.json()["max_tokens"] == 1024


def test_output_limit_helper() -> None:
    p = provider(ok, max_output_tokens=300)

    assert p.max_output_tokens == 300
    assert p.output_limit(request()) == 300
    assert p.output_limit(request(max_tokens=10)) == 10
    assert p.output_limit(request(max_tokens=301)) == 300


@pytest.mark.parametrize("bad", [0, -1])
def test_cap_below_one_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        ClaudeProvider(None, MODEL, max_output_tokens=bad)


def test_from_settings_uses_the_configured_cap() -> None:
    settings = Settings(_env_file=None, aether_cloud_max_output_tokens="777")

    assert ClaudeProvider.from_settings(settings).max_output_tokens == 777


def test_from_settings_defaults_to_4096() -> None:
    assert ClaudeProvider.from_settings(Settings(_env_file=None)).max_output_tokens == 4096


def test_cut_off_answer_keeps_partial_text_and_warns_without_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = usage_body(
        content=[{"type": "text", "text": "PARTIALANSWER cut off mid"}],
        stop_reason="max_tokens",
        output_tokens=64,
    )
    rec = Recorder(lambda _: httpx2.Response(200, json=body))

    with caplog.at_level(logging.WARNING, logger="aether.router.claude"):
        response = generate(provider(rec, max_output_tokens=64))

    assert response.finish_reason == "length"
    assert response.text == "PARTIALANSWER cut off mid"
    assert response.usage is not None and response.usage.output_tokens == 64
    [record] = [r for r in caplog.records if r.getMessage() == "Output cut off"]
    assert record.levelno == logging.WARNING
    assert record.max_tokens == 64  # type: ignore[attr-defined]
    assert record.output_tokens == 64  # type: ignore[attr-defined]
    assert record.stop_reason == "max_tokens"  # type: ignore[attr-defined]
    assert record.empty_text is False  # type: ignore[attr-defined]
    assert "PARTIALANSWER" not in caplog.text


def test_cut_off_with_empty_text_when_thinking_used_the_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Thinking counts toward max_tokens: it can use the whole budget, leaving no text.
    body = usage_body(
        content=[{"type": "thinking", "thinking": "", "signature": "sig"}],
        stop_reason="max_tokens",
        output_tokens=32,
    )
    rec = Recorder(lambda _: httpx2.Response(200, json=body))

    with caplog.at_level(logging.WARNING, logger="aether.router.claude"):
        response = generate(provider(rec, max_output_tokens=32))

    assert response.finish_reason == "length"
    assert response.text == ""
    [record] = [r for r in caplog.records if r.getMessage() == "Output cut off"]
    assert record.empty_text is True  # type: ignore[attr-defined]


def test_normal_answer_logs_no_cut_off_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aether.router.claude"):
        generate(provider(Recorder(ok)))

    assert "Output cut off" not in caplog.text
