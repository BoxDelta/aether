"""Failover (#19): when a provider call fails, at most one fallback, by the routing rules."""

import asyncio
import logging
from collections.abc import Callable

import pytest

from core.identity import Identity
from router.contract import (
    Message,
    ModelRequest,
    PrivacyLevel,
    PrivacyNotAllowed,
    ProviderBadResponse,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from router.routing import (
    Attempt,
    NoRoute,
    RouteDecision,
    RoutedResponse,
    RouteFailed,
    RouteOptions,
    Router,
)
from tests.fakes import FakeProvider, snapshot

PROMPT_TEXT = "PRIVATE-PROMPT-MARKER"
LEAKY_TEXT = "sk-ant-LEAKED-IN-EXCEPTION"  # gitleaks:allow


ROOM = 4.0
NO_ROOM = 0.5


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="aether.router")
    return caplog


def run(
    identity: Identity,
    providers: list[FakeProvider],
    *,
    privacy: PrivacyLevel = "personal",
    free_gib: float = ROOM,
    timeout_s: float = 60.0,
    **options: object,
) -> RoutedResponse:
    router = Router(providers, identity, probe=lambda: snapshot(free_gib))
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
    local, cloud = FakeProvider("ollama", "local", error=error), FakeProvider("claude", "cloud")

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
    local = FakeProvider(
        "ollama", "local", error=ProviderTimeout("ollama", "Ollama did not respond")
    )
    cloud = FakeProvider("claude", "cloud")

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
    local = FakeProvider("ollama", "local", error=ProviderUnavailable("ollama", "cannot connect"))
    cloud = FakeProvider(
        "claude", "cloud", error=ProviderUnavailable("claude", "rate limited (HTTP 429)")
    )

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
    local = FakeProvider("ollama", "local")
    cloud = FakeProvider("claude", "cloud", finish_reason="refusal")

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
    local = FakeProvider("ollama", "local", finish_reason="refusal")
    cloud = FakeProvider("claude", "cloud")

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
    local, cloud = FakeProvider("ollama", "local", error=error), FakeProvider("claude", "cloud")

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    assert info.value.reason == (
        f"no failover: ollama error is not retryable ({type(error).__name__})"
    )
    assert cloud.calls == 0
    assert len(info.value.attempts) == 1


def test_real_timeout_from_contract_fails_over(identity: Identity) -> None:
    # Provider.generate turns the deadline into ProviderTimeout (retryable).
    local, cloud = FakeProvider("ollama", "local", delay_s=1.0), FakeProvider("claude", "cloud")

    result = run(identity, [local, cloud], allow_cloud=True, timeout_s=0.05)

    assert result.failed_first is not None
    assert result.failed_first.error_type == "ProviderTimeout"
    assert result.decision.provider == "claude"


def test_adapter_bug_is_scrubbed_and_not_retried(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    # A raw exception carrying sensitive text: the contract turns it into a
    # non-retryable ProviderBadResponse with the type name only.
    local = FakeProvider("ollama", "local", error=RuntimeError(f"boom {LEAKY_TEXT} {PROMPT_TEXT}"))
    cloud = FakeProvider("claude", "cloud")

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
    local = FakeProvider("ollama", "local", error=error if failing == "ollama" else None)
    cloud = FakeProvider("claude", "cloud", error=error if failing == "claude" else None)

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True, override=override)

    assert info.value.reason == f"no failover: manual override '{override}'"
    assert (local.calls, cloud.calls) == ((1, 0) if override == "ollama" else (0, 1))


# --- rule: sensitive and secret --------------------------------------------------


@pytest.mark.parametrize("error", RETRYABLE, ids=lambda e: e.message)
def test_sensitive_never_fails_over_to_cloud(identity: Identity, error: ProviderError) -> None:
    local, cloud = FakeProvider("ollama", "local", error=error), FakeProvider("claude", "cloud")

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], privacy="sensitive", allow_cloud=True)

    assert "cloud may not receive sensitive data" in info.value.reason
    assert cloud.calls == 0 and cloud.checks == 0


def test_secret_never_routed(identity: Identity) -> None:
    local, cloud = FakeProvider("ollama", "local"), FakeProvider("claude", "cloud")

    with pytest.raises(NoRoute, match="secret data is never sent"):
        run(identity, [local, cloud], privacy="secret", allow_cloud=True)

    assert local.calls == cloud.calls == 0
    assert local.checks == cloud.checks == 0


# --- rule: one fallback, through the routing rules, no loops ---------------------


def test_at_most_one_fallback_even_with_more_providers(
    identity: Identity, logs: pytest.LogCaptureFixture
) -> None:
    providers = [
        FakeProvider("ollama", "local", error=ProviderUnavailable("ollama", "down")),
        FakeProvider("ollama2", "local", error=ProviderUnavailable("ollama2", "down")),
        FakeProvider("claude", "cloud", error=ProviderUnavailable("claude", "down")),
    ]

    with pytest.raises(RouteFailed) as info:
        run(identity, providers, allow_cloud=True)

    assert sum(p.calls for p in providers) == 2
    assert len(info.value.attempts) == 2
    assert len(records(logs, "Failover")) == 1


def test_fallback_is_rechecked_by_routing_rules(identity: Identity) -> None:
    # Cloud is allowed but not available at failover time: no blind retry.
    local = FakeProvider("ollama", "local", error=ProviderTimeout("ollama", "slow"))
    cloud = FakeProvider("claude", "cloud", available=False)

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    assert info.value.reason == "no failover: no provider: local unavailable; cloud unavailable"
    assert cloud.calls == 0


def test_cloud_error_can_fail_over_to_local(identity: Identity) -> None:
    # Errors (not refusals) may fail over cloud -> local; local may take personal data.
    local = FakeProvider("ollama", "local")
    cloud = FakeProvider(
        "claude", "cloud", error=ProviderUnavailable("claude", "overloaded (HTTP 529)")
    )

    result = run(identity, [local, cloud], allow_cloud=True, free_gib=NO_ROOM)

    assert result.decision.provider == "ollama"
    assert "local without VRAM headroom" in result.decision.reason
    assert result.failed_first is not None and result.failed_first.provider == "claude"


def test_on_decision_reports_first_and_fallback(identity: Identity) -> None:
    local = FakeProvider("ollama", "local", error=ProviderTimeout("ollama", "slow"))
    cloud = FakeProvider("claude", "cloud")
    seen: list[str] = []
    router = Router([local, cloud], identity, probe=lambda: snapshot(ROOM))
    request = ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),))

    asyncio.run(
        router.handle(
            request, RouteOptions(allow_cloud=True), on_decision=lambda d: seen.append(d.provider)
        )
    )

    assert seen == ["ollama", "claude"]


def test_on_decision_not_called_for_no_route(identity: Identity) -> None:
    seen: list[str] = []
    router = Router([FakeProvider("ollama", "local")], identity, probe=lambda: snapshot(ROOM))
    request = ModelRequest(messages=(Message(role="user", content="x"),), privacy="secret")

    with pytest.raises(NoRoute):
        asyncio.run(router.handle(request, RouteOptions(), on_decision=lambda d: seen.append("x")))

    assert seen == []


# --- no-fallback paths after a failed call (#32) ------------------------------------

NO_FALLBACK = [
    # (first error, extra route options, providers available): one case per reason.
    pytest.param(ProviderBadResponse("ollama", "bad"), {"allow_cloud": True}, id="not-retryable"),
    pytest.param(
        ProviderUnavailable("ollama", "down"),
        {"allow_cloud": True, "override": "ollama"},
        id="override",
    ),
    pytest.param(ProviderUnavailable("ollama", "down"), {}, id="no-other-route"),
]


@pytest.mark.parametrize(("error", "options"), NO_FALLBACK)
def test_no_fallback_chains_the_provider_error_and_reports_one_decision(
    identity: Identity, error: ProviderError, options: dict[str, object]
) -> None:
    local, cloud = FakeProvider("ollama", "local", error=error), FakeProvider("claude", "cloud")
    seen: list[str] = []
    router = Router([local, cloud], identity, probe=lambda: snapshot(ROOM))
    request = ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),))

    with pytest.raises(RouteFailed) as info:
        asyncio.run(
            router.handle(
                request,
                RouteOptions.model_validate(options),
                on_decision=lambda d: seen.append(d.provider),
            )
        )

    assert seen == ["ollama"]  # no fallback decision is reported
    assert cloud.calls == 0
    assert [a.provider for a in info.value.attempts] == ["ollama"]
    # Raised "from" the first provider error, with that same error as context.
    assert info.value.__cause__ is error
    assert info.value.__context__ is error
    assert info.value.__suppress_context__


def test_failed_fallback_chains_the_second_provider_error(identity: Identity) -> None:
    second = ProviderUnavailable("claude", "down")
    local = FakeProvider("ollama", "local", error=ProviderUnavailable("ollama", "down"))
    cloud = FakeProvider("claude", "cloud", error=second)

    with pytest.raises(RouteFailed) as info:
        run(identity, [local, cloud], allow_cloud=True)

    assert info.value.__cause__ is second


# --- on_attempt_failed (#30): reports each failure as it happens, never steers -------

CALLBACK_CASES = [
    # (local error, cloud error, options): failover succeeds / fails / no fallback.
    pytest.param(ProviderTimeout("ollama", "slow"), None, {"allow_cloud": True}, id="succeeds"),
    pytest.param(
        ProviderTimeout("ollama", "slow"),
        ProviderUnavailable("claude", "down"),
        {"allow_cloud": True},
        id="fails",
    ),
    pytest.param(
        ProviderBadResponse("ollama", "bad"), None, {"allow_cloud": True}, id="no-fallback"
    ),
]


def handle_with(
    identity: Identity,
    local_error: ProviderError,
    cloud_error: ProviderError | None,
    options: dict[str, object],
    on_attempt_failed: Callable[[Attempt], None],
    seen: list[tuple[str, str]],
) -> tuple[object, list[int]]:
    local = FakeProvider("ollama", "local", error=local_error)
    cloud = FakeProvider("claude", "cloud", error=cloud_error)
    router = Router([local, cloud], identity, probe=lambda: snapshot(ROOM))
    request = ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),))
    try:
        result = asyncio.run(
            router.handle(
                request,
                RouteOptions.model_validate(options),
                on_decision=lambda d: seen.append(("decision", d.provider)),
                on_attempt_failed=on_attempt_failed,
            )
        )
        outcome: object = ("ok", result.decision.provider, result.failed_first)
    except RouteFailed as exc:
        chain = (exc.__cause__, exc.__context__, exc.__suppress_context__)
        outcome = ("RouteFailed", exc.reason, exc.attempts, chain)
    return outcome, [local.calls, cloud.calls]


@pytest.mark.parametrize(("local_error", "cloud_error", "options"), CALLBACK_CASES)
def test_on_attempt_failed_reports_each_failure_when_it_happens(
    identity: Identity,
    local_error: ProviderError,
    cloud_error: ProviderError | None,
    options: dict[str, object],
) -> None:
    seen: list[tuple[str, str]] = []

    handle_with(
        identity,
        local_error,
        cloud_error,
        options,
        lambda a: seen.append(("failed", a.provider)),
        seen,
    )

    expected = [("decision", "ollama"), ("failed", "ollama")]
    if local_error.retryable:
        expected.append(("decision", "claude"))
        if cloud_error is not None:
            expected.append(("failed", "claude"))
    assert seen == expected


@pytest.mark.parametrize(("local_error", "cloud_error", "options"), CALLBACK_CASES)
def test_raising_on_attempt_failed_never_changes_routing(
    identity: Identity,
    logs: pytest.LogCaptureFixture,
    local_error: ProviderError,
    cloud_error: ProviderError | None,
    options: dict[str, object],
) -> None:
    def broken(_: Attempt) -> None:
        raise RuntimeError(f"hook broke {LEAKY_TEXT} {PROMPT_TEXT}")

    quiet = handle_with(identity, local_error, cloud_error, options, lambda _: None, [])
    logs.clear()
    loud = handle_with(identity, local_error, cloud_error, options, broken, [])

    assert loud == quiet  # same result or RouteFailed (reason, attempts, chain), same calls
    warnings = records(logs, "Attempt callback failed")
    assert len(warnings) == (2 if cloud_error is not None else 1)
    assert {(w.levelname, w.__dict__["callback_error_type"]) for w in warnings} == {
        ("WARNING", "RuntimeError")
    }
    assert_no_leaks(logs)


# --- on_decision (#48): guarded like on_attempt_failed --------------------------------

DECISION_CASES = [
    # (local error, cloud error, options, decisions reported)
    pytest.param(None, None, {"allow_cloud": True}, 1, id="no-failover"),
    pytest.param(ProviderTimeout("ollama", "slow"), None, {"allow_cloud": True}, 2, id="succeeds"),
    pytest.param(
        ProviderTimeout("ollama", "slow"),
        ProviderUnavailable("claude", "down"),
        {"allow_cloud": True},
        2,
        id="fails",
    ),
    pytest.param(
        ProviderBadResponse("ollama", "bad"), None, {"allow_cloud": True}, 1, id="no-fallback"
    ),
]


def outcome_with_on_decision(
    identity: Identity,
    local_error: ProviderError | None,
    cloud_error: ProviderError | None,
    options: dict[str, object],
    on_decision: Callable[[RouteDecision], None],
) -> tuple[object, list[int]]:
    local = FakeProvider("ollama", "local", error=local_error)
    cloud = FakeProvider("claude", "cloud", error=cloud_error)
    router = Router([local, cloud], identity, probe=lambda: snapshot(ROOM))
    request = ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),))
    try:
        result = asyncio.run(
            router.handle(request, RouteOptions.model_validate(options), on_decision=on_decision)
        )
        outcome: object = ("ok", result.decision, result.response.text, result.failed_first)
    except RouteFailed as exc:
        chain = (exc.__cause__, exc.__context__, exc.__suppress_context__)
        outcome = ("RouteFailed", exc.reason, exc.attempts, chain)
    return outcome, [local.calls, cloud.calls]


@pytest.mark.parametrize(("local_error", "cloud_error", "options", "decisions"), DECISION_CASES)
def test_raising_on_decision_never_changes_routing(
    identity: Identity,
    logs: pytest.LogCaptureFixture,
    local_error: ProviderError | None,
    cloud_error: ProviderError | None,
    options: dict[str, object],
    decisions: int,
) -> None:
    def broken(_: RouteDecision) -> None:
        raise RuntimeError(f"hook broke {LEAKY_TEXT} {PROMPT_TEXT}")

    quiet = outcome_with_on_decision(identity, local_error, cloud_error, options, lambda _: None)
    logs.clear()
    loud = outcome_with_on_decision(identity, local_error, cloud_error, options, broken)

    assert loud == quiet  # same result or RouteFailed (reason, attempts, chain), same calls
    warnings = records(logs, "Decision callback failed")
    assert len(warnings) == decisions
    assert [(w.levelname, w.__dict__["callback_error_type"]) for w in warnings] == [
        ("WARNING", "RuntimeError")
    ] * decisions
    assert [w.__dict__["provider"] for w in warnings] == ["ollama", "claude"][:decisions]
    assert_no_leaks(logs)


def test_on_decision_cancellation_still_propagates(identity: Identity) -> None:
    def cancelled(_: RouteDecision) -> None:
        raise asyncio.CancelledError

    local, cloud = FakeProvider("ollama", "local"), FakeProvider("claude", "cloud")
    router = Router([local, cloud], identity, probe=lambda: snapshot(ROOM))
    request = ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router.handle(request, RouteOptions(), on_decision=cancelled))
    assert local.calls == 0  # cancelled before the call
