import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pydantic import ValidationError

from router.contract import (
    PRIVACY_ORDER,
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    PrivacyNotAllowed,
    Provider,
    ProviderBadResponse,
    ProviderError,
    ProviderInfo,
    ProviderTimeout,
    ProviderUnavailable,
    privacy_rank,
)

FAKE_KEY = "sk-ant-test-contract-0123456789"  # gitleaks:allow

Behaviour = Callable[[ModelRequest], Awaitable[ModelResponse]]

LOCAL = ProviderInfo(name="fake-local", kind="local", default_model="m1", max_privacy="sensitive")
CLOUD = ProviderInfo(name="fake-cloud", kind="cloud", default_model="c1", max_privacy="personal")


class FakeProvider(Provider):
    def __init__(self, info: ProviderInfo, behaviour: Behaviour | None = None) -> None:
        super().__init__(info)
        self.calls: list[ModelRequest] = []
        self._behaviour = behaviour

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self._behaviour is not None:
            return await self._behaviour(request)
        return ModelResponse(
            text=f"echo: {request.messages[-1].content}",
            provider=self.info.name,
            model=request.model or self.info.default_model,
            kind=self.info.kind,
        )

    async def check_available(self) -> bool:
        return True


def request(privacy: PrivacyLevel = "personal", **kwargs: object) -> ModelRequest:
    return ModelRequest(
        messages=(Message(role="user", content="hi"),),
        privacy=privacy,
        **kwargs,  # type: ignore[arg-type]
    )


def run(provider: Provider, req: ModelRequest) -> ModelResponse:
    return asyncio.run(provider.generate(req))


# --- success -------------------------------------------------------------------


def test_generate_returns_response_with_latency() -> None:
    provider = FakeProvider(LOCAL)

    response = run(provider, request(model="m2"))

    assert response.text == "echo: hi"
    assert response.provider == "fake-local"
    assert response.model == "m2"
    assert response.kind == "local"
    assert response.latency_ms >= 0
    assert len(provider.calls) == 1


# --- privacy -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("info", "privacy", "allowed"),
    [
        (LOCAL, "public", True),
        (LOCAL, "personal", True),
        (LOCAL, "sensitive", True),
        (LOCAL, "secret", False),
        (CLOUD, "public", True),
        (CLOUD, "personal", True),
        (CLOUD, "sensitive", False),
        (CLOUD, "secret", False),
    ],
)
def test_privacy_gate(info: ProviderInfo, privacy: PrivacyLevel, allowed: bool) -> None:
    provider = FakeProvider(info)

    assert provider.allows(privacy) is allowed
    if allowed:
        run(provider, request(privacy))
        assert len(provider.calls) == 1
    else:
        with pytest.raises(PrivacyNotAllowed) as caught:
            run(provider, request(privacy))
        assert caught.value.retryable is False
        assert provider.calls == []  # refused before the adapter is called


def test_secret_is_refused_even_by_most_permissive_provider() -> None:
    public_only = ProviderInfo(name="p", kind="local", default_model="m", max_privacy="public")

    for info in (LOCAL, CLOUD, public_only):
        assert not FakeProvider(info).allows("secret")


def test_cloud_provider_cannot_be_configured_for_sensitive() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        ProviderInfo(name="c", kind="cloud", default_model="m", max_privacy="sensitive")


def test_provider_info_rejects_secret_max_privacy() -> None:
    with pytest.raises(ValidationError):
        ProviderInfo(name="l", kind="local", default_model="m", max_privacy="secret")


def test_privacy_order() -> None:
    assert [privacy_rank(level) for level in PRIVACY_ORDER] == [0, 1, 2, 3]


def test_unclassified_request_defaults_to_personal() -> None:
    assert ModelRequest(messages=(Message(role="user", content="x"),)).privacy == "personal"


# --- timeouts and errors -------------------------------------------------------


def test_timeout_is_enforced() -> None:
    async def slow(_: ModelRequest) -> ModelResponse:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    with pytest.raises(ProviderTimeout) as caught:
        run(FakeProvider(LOCAL, slow), request(timeout_s=0.05))

    assert caught.value.retryable is True
    assert caught.value.provider == "fake-local"


def test_contract_errors_pass_through_unchanged() -> None:
    error = ProviderUnavailable("fake-local", "connection refused")

    async def down(_: ModelRequest) -> ModelResponse:
        raise error

    with pytest.raises(ProviderUnavailable) as caught:
        run(FakeProvider(LOCAL, down), request())

    assert caught.value is error
    assert caught.value.retryable is True


def test_unexpected_exception_becomes_bad_response_without_its_message() -> None:
    async def leaky(_: ModelRequest) -> ModelResponse:
        raise RuntimeError(f"401 for key {FAKE_KEY}")

    with pytest.raises(ProviderBadResponse) as caught:
        run(FakeProvider(CLOUD, leaky), request())

    error = caught.value
    assert error.retryable is False
    assert "RuntimeError" in str(error)
    assert FAKE_KEY not in str(error)
    assert FAKE_KEY not in repr(error)
    assert isinstance(error.__cause__, RuntimeError)  # kept for redacted server logs


def test_mismatched_provider_in_response_is_rejected() -> None:
    async def wrong(_: ModelRequest) -> ModelResponse:
        return ModelResponse(text="x", provider="someone-else", model="m", kind="local")

    with pytest.raises(ProviderBadResponse, match="mismatched"):
        run(FakeProvider(LOCAL, wrong), request())


def test_retryable_defaults_and_override() -> None:
    assert ProviderTimeout("p", "x").retryable is True
    assert ProviderUnavailable("p", "x").retryable is True
    assert ProviderUnavailable("p", "no key", retryable=False).retryable is False
    assert ProviderBadResponse("p", "x").retryable is False
    assert isinstance(PrivacyNotAllowed("p", "x"), ProviderError)


# --- request validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"messages": ()},
        {"messages": (Message(role="user", content="x"),), "max_tokens": 0},
        {"messages": (Message(role="user", content="x"),), "temperature": 2.5},
        {"messages": (Message(role="user", content="x"),), "timeout_s": 0},
        {"messages": (Message(role="user", content="x"),), "privacy": "top-secret"},
        {"messages": (Message(role="user", content="x"),), "unknown": 1},
    ],
)
def test_invalid_requests_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["system", "tool"])
def test_messages_only_allow_user_and_assistant(role: str) -> None:
    # The system prompt is a separate field so the identity can't be spoofed via messages.
    with pytest.raises(ValidationError):
        Message(role=role, content="x")  # type: ignore[arg-type]


def test_empty_message_content_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Message(role="user", content="")


def test_contract_types_are_immutable() -> None:
    req = request()

    with pytest.raises(ValidationError):
        req.privacy = "public"  # type: ignore[misc]


def test_provider_must_implement_abstract_methods() -> None:
    class Incomplete(Provider):
        pass

    with pytest.raises(TypeError):
        Incomplete(LOCAL)  # type: ignore[abstract]
