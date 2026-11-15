"""Failover (#19): when a provider call fails, at most one fallback, by the routing rules."""

import asyncio
import logging

import pytest

from core.identity import Identity, load_identity
from router.contract import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    PrivacyNotAllowed,
    Provider,
    ProviderBadResponse,
    ProviderError,
    ProviderInfo,
    ProviderKind,
    ProviderTimeout,
    ProviderUnavailable,
)
from router.hardware import GpuState, HardwareSnapshot
from router.routing import NoRoute, RoutedResponse, RouteFailed, RouteOptions, Router

GIB = 1024**3
PROMPT_TEXT = "PRIVATE-PROMPT-MARKER"
LEAKY_TEXT = "sk-ant-LEAKED-IN-EXCEPTION"  # gitleaks:allow


class Fake(Provider):
    def __init__(
        self,
        name: str,
        kind: ProviderKind,
        *,
        error: ProviderError | Exception | None = None,
        finish_reason: FinishReason = "stop",
        available: bool = True,
        delay_s: float = 0.0,
    ) -> None:
        super().__init__(
            ProviderInfo(
                name=name,
                kind=kind,
                default_model=f"{name}-model",
                max_privacy="sensitive" if kind == "local" else "personal",
            )
        )
        self.error = error
        self.finish_reason = finish_reason
        self.available = available
        self.delay_s = delay_s
        self.calls = 0
        self.checks = 0

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        refused = self.finish_reason == "refusal"
        return ModelResponse(
            text="" if refused else f"answer from {self.name}",
            provider=self.name,
            model=self.info.default_model,
            kind=self.info.kind,
            finish_reason=self.finish_reason,
        )

    async def check_available(self) -> bool:
        self.checks += 1
        return self.available


def probe(free_gib: float) -> HardwareSnapshot:
    return HardwareSnapshot(GpuState("nvml", 8 * GIB, int(free_gib * GIB), 10), 16 * GIB)


ROOM = 4.0
NO_ROOM = 0.5


@pytest.fixture
def identity() -> Identity:
    return load_identity()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="aether.router")
    return caplog


def run(
    identity: Identity,
    providers: list[Fake],
    *,
    privacy: PrivacyLevel = "personal",
    free_gib: float = ROOM,
    timeout_s: float = 60.0,
    **options: object,
) -> RoutedResponse:
    router = Router(providers, identity, probe=lambda: probe(free_gib))
    request = ModelRequest(
        messages=(Message(role="user", content=PROMPT_TEXT),),
        privacy=privacy,
        timeout_s=timeout_s,
    )
    return asyncio.run(router.handle(request, RouteOptions.model_validate(options)))


def records(logs: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in logs.records if r.getMessage() == message]


def assert_no_leaks(logs: pytest.LogCaptureFixture) -> None:
    for r in logs.records:
        dump = str(r.__dict__)
        assert PROMPT_TEXT not in dump
        assert LEAKY_TEXT not in dump


RETRYABLE = [
    ProviderUnavailable("ollama", "cannot connect to Ollama"),
    ProviderTimeout("ollama", "Ollama did not respond in time"),
    ProviderUnavailable("ollama", "Ollama server error (HTTP 503)"),
]
NOT_RETRYABLE = [
    ProviderUnavailable("ollama", "model 'x' not found", retryable=False),
    ProviderBadResponse("ollama", "response has no message content"),
    PrivacyNotAllowed("ollama", "request privacy 'sensitive' exceeds 'personal'"),
]


# --- issue acceptance ------------------------------------------------------------


@pytest.mark.parametrize("error", RETRYABLE, ids=lambda e: e.message)
def test_local_only_never_reaches_claude_even_when_ollama_fails(
    identity: Identity, logs: pytest.LogCaptureFixture, error: ProviderError
) -> None:
    local, cloud = Fake("ollama", "local", error=error), Fake("claude", "cloud")

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud])  # allow_cloud defaults to false

    assert local.calls == 1
    assert cloud.calls == 0 and cloud.checks == 0
    assert "cloud not allowed for this request" in info.value.reason
    assert [a.provider for a in info.value.attempts] == ["ollama"]
    assert len(records(logs, "No failover")) == 1


def test_local_failure_fails_over_to_cloud_with_reason_logged(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    local = Fake("ollama", "local", error=ProviderTimeout("ollama", "Ollama did not respond"))
    cloud = Fake("claude", "cloud")

    result = run(identity, [local, cloud], allow_cloud=True)

    assert result.response.text == "answer from claude"
    assert result.decision.provider == "claude"
    assert result.failed_first is not None
    assert result.failed_first.provider == "ollama"
    assert result.failed_first.error_type == "ProviderTimeout"
    assert local.calls == 1 and cloud.calls == 1

    (failover,) = records(logs, "Failover")
    assert failover.provider == "ollama"  # type: ignore[attr-defined]
    assert failover.to_provider == "claude"  # type: ignore[attr-defined]
    assert failover.error == "Ollama did not respond"  # type: ignore[attr-defined]
    assert failover.reason.startswith("ollama failed (ProviderTimeout); cloud: ")  # type: ignore[attr-defined]
    # Both calls went through call_with_identity.
    assert len(records(logs, "Model call failed")) == 1
    assert len(records(logs, "Model call")) == 1
    assert_no_leaks(logs)


def test_all_providers_fail_gives_explicit_error(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    local = Fake("ollama", "local", error=ProviderUnavailable("ollama", "cannot connect"))
    cloud = Fake("claude", "cloud", error=ProviderUnavailable("claude", "rate limited (HTTP 429)"))

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    assert info.value.reason == "fallback claude also failed (ProviderUnavailable)"
    assert [(a.provider, a.message) for a in info.value.attempts] == [
        ("ollama", "cannot connect"),
        ("claude", "rate limited (HTTP 429)"),
    ]
    assert len(records(logs, "Failover")) == 1
    assert len(records(logs, "Failover failed")) == 1


def test_cloud_refusal_is_final_and_local_not_called(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    local = Fake("ollama", "local")
    cloud = Fake("claude", "cloud", finish_reason="refusal")

    # No VRAM headroom, so cloud is chosen first.
    result = run(identity, [local, cloud], allow_cloud=True, free_gib=NO_ROOM)

    assert result.response.finish_reason == "refusal"
    assert result.decision.provider == "claude"
    assert result.failed_first is None
    assert local.calls == 0
    assert records(logs, "Failover") == [] and records(logs, "No failover") == []


def test_local_refusal_is_final_and_cloud_not_called(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    local = Fake("ollama", "local", finish_reason="refusal")
    cloud = Fake("claude", "cloud")

    result = run(identity, [local, cloud], allow_cloud=True)

    assert result.response.finish_reason == "refusal"
    assert result.decision.provider == "ollama"
    assert cloud.calls == 0
    assert records(logs, "Failover") == []


# --- rule: only retryable errors -------------------------------------------------


@pytest.mark.parametrize("error", NOT_RETRYABLE, ids=lambda e: type(e).__name__)
def test_non_retryable_error_does_not_fail_over(
    identity: Identity, logs: pytest.LogCaptureFixture, error: ProviderError
) -> None:
    local, cloud = Fake("ollama", "local", error=error), Fake("claude", "cloud")

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    assert info.value.reason == (
        f"no failover: ollama error is not retryable ({type(error).__name__})"
    )
    assert cloud.calls == 0
    assert len(info.value.attempts) == 1


def test_real_timeout_from_contract_fails_over(identity: Identity) -> None:
    # Provider.generate turns the deadline into ProviderTimeout (retryable).
    local, cloud = Fake("ollama", "local", delay_s=1.0), Fake("claude", "cloud")

    result = run(identity, [local, cloud], allow_cloud=True, timeout_s=0.05)

    assert result.failed_first is not None
    assert result.failed_first.error_type == "ProviderTimeout"
    assert result.decision.provider == "claude"


def test_adapter_bug_is_scrubbed_and_not_retried(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    # A raw exception carrying sensitive text: the contract turns it into a
    # non-retryable ProviderBadResponse with the type name only.
    local = Fake("ollama", "local", error=RuntimeError(f"boom {LEAKY_TEXT} {PROMPT_TEXT}"))
    cloud = Fake("claude", "cloud")

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    (attempt,) = info.value.attempts
    assert attempt.error_type == "ProviderBadResponse"
    assert attempt.message == "unexpected RuntimeError from adapter"
    assert LEAKY_TEXT not in str(info.value) and LEAKY_TEXT not in str(attempt)
    assert cloud.calls == 0
    assert_no_leaks(logs)


# --- rule: override never fails over -------------------------------------------


@pytest.mark.parametrize(
    ("override", "failing"),
    [("ollama", "ollama"), ("claude", "claude")],
)
def test_override_never_fails_over(identity: Identity, override: str, failing: str) -> None:
    error = ProviderUnavailable(failing, "cannot connect")  # retryable
    local = Fake("ollama", "local", error=error if failing == "ollama" else None)
    cloud = Fake("claude", "cloud", error=error if failing == "claude" else None)

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True, override=override)

    assert info.value.reason == f"no failover: manual override '{override}'"
    assert (local.calls, cloud.calls) == ((1, 0) if override == "ollama" else (0, 1))


# --- rule: sensitive and secret --------------------------------------------------


@pytest.mark.parametrize("error", RETRYABLE, ids=lambda e: e.message)
def test_sensitive_never_fails_over_to_cloud(identity: Identity, error: ProviderError) -> None:
    local, cloud = Fake("ollama", "local", error=error), Fake("claude", "cloud")

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], privacy="sensitive", allow_cloud=True)

    assert "cloud may not receive sensitive data" in info.value.reason
    assert cloud.calls == 0 and cloud.checks == 0


def test_secret_never_routed(identity: Identity) -> None:
    local, cloud = Fake("ollama", "local"), Fake("claude", "cloud")

    with pytest.raises(NoRoute, match="secret data is never sent"):
        run(identity, [local, cloud], privacy="secret", allow_cloud=True)

    assert local.calls == cloud.calls == 0
    assert local.checks == cloud.checks == 0


# --- rule: one fallback, through the routing rules, no loops ---------------------


def test_at_most_one_fallback_even_with_more_providers(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    providers = [
        Fake("ollama", "local", error=ProviderUnavailable("ollama", "down")),
        Fake("ollama2", "local", error=ProviderUnavailable("ollama2", "down")),
        Fake("claude", "cloud", error=ProviderUnavailable("claude", "down")),
    ]

    with pytest.raises(RouteFailed) as info:
        run(identity, providers, allow_cloud=True)

    assert sum(p.calls for p in providers) == 2
    assert len(info.value.attempts) == 2
    assert len(records(logs, "Failover")) == 1


def test_fallback_is_rechecked_by_routing_rules(identity: Identity) -> None:
    # Cloud is allowed but not available at failover time: no blind retry.
    local = Fake("ollama", "local", error=ProviderTimeout("ollama", "slow"))
    cloud = Fake("claude", "cloud", available=False)

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    assert info.value.reason == "no failover: no provider: local unavailable; cloud unavailable"
    assert cloud.calls == 0


def test_cloud_error_can_fail_over_to_local(identity: Identity) -> None:
    # Errors (not refusals) may fail over cloud -> local; local may take personal data.
    local = Fake("ollama", "local")
    cloud = Fake("claude", "cloud", error=ProviderUnavailable("claude", "overloaded (HTTP 529)"))

    result = run(identity, [local, cloud], allow_cloud=True, free_gib=NO_ROOM)

    assert result.decision.provider == "ollama"
    assert "local without VRAM headroom" in result.decision.reason
    assert result.failed_first is not None and result.failed_first.provider == "claude"


def test_on_decision_reports_first_and_fallback(identity: Identity) -> None:
    local = Fake("ollama", "local", error=ProviderTimeout("ollama", "slow"))
    cloud = Fake("claude", "cloud")
    seen: list[str] = []
    router = Router([local, cloud], identity, probe=lambda: probe(ROOM))
    request = ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),))

    asyncio.run(
        router.handle(
            request, RouteOptions(allow_cloud=True), on_decision=lambda d: seen.append(d.provider)
        )
    )

    assert seen == ["ollama", "claude"]


def test_on_decision_not_called_for_no_route(identity: Identity) -> None:
    seen: list[str] = []
    router = Router([Fake("ollama", "local")], identity, probe=lambda: probe(ROOM))
    request = ModelRequest(messages=(Message(role="user", content="x"),), privacy="secret")

    with pytest.raises(NoRoute):
        asyncio.run(router.handle(request, RouteOptions(), on_decision=lambda d: seen.append("x")))

    assert seen == []
