"""Routing rules (#18): table-driven decisions, invariants, and the Router."""

import asyncio
import itertools
import logging
import threading
from collections.abc import Callable

import pytest
from pydantic import SecretStr, ValidationError

from core.config import Settings
from core.identity import Identity
from router.contract import (
    PRIVACY_ORDER,
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    Provider,
    ProviderInfo,
    ProviderKind,
    ProviderUnavailable,
)
from router.hardware import HardwareSnapshot, Headroom
from router.routing import (
    Candidate,
    NoRoute,
    RouteFailed,
    RouteOptions,
    Router,
    choose_route,
)
from tests.fakes import CHECK, snapshot

ROOM = Headroom(True, "free VRAM 4096 MiB meets reserve 1536 MiB")
NO_ROOM = Headroom(False, "free VRAM 512 MiB below reserve 1536 MiB")
UNKNOWN = Headroom(False, "GPU state unknown")
PROMPT_TEXT = "PRIVATE-PROMPT-MARKER"

# Provider privacy ceilings as configured in the real adapters.
LOCAL_MAX = PRIVACY_ORDER.index("sensitive")
CLOUD_MAX = PRIVACY_ORDER.index("personal")


def cands(
    privacy: PrivacyLevel, *, local: bool | None = True, cloud: bool | None = True
) -> list[Candidate]:
    """local/cloud: True available, False unavailable, None not configured."""
    rank = PRIVACY_ORDER.index(privacy)
    out = []
    if local is not None:
        out.append(Candidate("ollama", "local", privacy != "secret" and rank <= LOCAL_MAX, local))
    if cloud is not None:
        out.append(Candidate("claude", "cloud", rank <= CLOUD_MAX, cloud))
    return out


# --- decision table --------------------------------------------------------------

CLOUD_OK = RouteOptions(allow_cloud=True)
LOCAL_ONLY = RouteOptions()

ROUTES: list[tuple[str, PrivacyLevel, RouteOptions, list[Candidate], Headroom | None, str, str]] = [
    # id, privacy, options, candidates, headroom, expected provider, reason contains
    ("local-has-room", "personal", CLOUD_OK, cands("personal"), ROOM, "ollama", "local preferred"),
    ("public-local-first", "public", CLOUD_OK, cands("public"), ROOM, "ollama", "local preferred"),
    (
        "no-room-goes-cloud",
        "personal",
        CLOUD_OK,
        cands("personal"),
        NO_ROOM,
        "claude",
        "cloud: no local VRAM headroom (free VRAM 512 MiB below reserve",
    ),
    (
        "unknown-gpu-goes-cloud",
        "personal",
        CLOUD_OK,
        cands("personal"),
        UNKNOWN,
        "claude",
        "GPU state unknown",
    ),
    (
        "local-down-goes-cloud",
        "personal",
        CLOUD_OK,
        cands("personal", local=False),
        ROOM,
        "claude",
        "cloud: local unavailable",
    ),
    (
        "no-local-configured",
        "personal",
        CLOUD_OK,
        cands("personal", local=None),
        None,
        "claude",
        "no local provider configured",
    ),
    (
        "allow-cloud-false-no-room-stays-local",
        "personal",
        LOCAL_ONLY,
        cands("personal"),
        NO_ROOM,
        "ollama",
        "local without VRAM headroom (free VRAM 512 MiB below reserve 1536 MiB); "
        "cloud not allowed for this request",
    ),
    (
        "sensitive-no-room-stays-local",
        "sensitive",
        CLOUD_OK,
        cands("sensitive"),
        NO_ROOM,
        "ollama",
        "cloud may not receive sensitive data",
    ),
    (
        "cloud-down-no-room-stays-local",
        "personal",
        CLOUD_OK,
        cands("personal", cloud=False),
        NO_ROOM,
        "ollama",
        "cloud unavailable",
    ),
    (
        "override-cloud",
        "personal",
        RouteOptions(allow_cloud=True, override="claude"),
        cands("personal"),
        ROOM,
        "claude",
        "manual override: claude",
    ),
    (
        "override-local-no-room",
        "personal",
        RouteOptions(allow_cloud=True, override="ollama"),
        cands("personal"),
        NO_ROOM,
        "ollama",
        "manual override: ollama (desktop VRAM reserve not met",
    ),
    (
        "override-local-sensitive",
        "sensitive",
        RouteOptions(override="ollama"),
        cands("sensitive"),
        ROOM,
        "ollama",
        "manual override: ollama",
    ),
]


@pytest.mark.parametrize(
    ("privacy", "options", "candidates", "headroom", "provider", "reason"),
    [pytest.param(*row[1:], id=row[0]) for row in ROUTES],
)
def test_routes(
    privacy: PrivacyLevel,
    options: RouteOptions,
    candidates: list[Candidate],
    headroom: Headroom | None,
    provider: str,
    reason: str,
) -> None:
    decision = choose_route(privacy, options, candidates, headroom)
    assert decision.provider == provider
    assert reason in decision.reason


REFUSALS: list[tuple[str, PrivacyLevel, RouteOptions, list[Candidate], Headroom | None, str]] = [
    ("secret", "secret", CLOUD_OK, cands("secret"), ROOM, "secret data is never sent"),
    (
        "secret-with-override",
        "secret",
        RouteOptions(allow_cloud=True, override="ollama"),
        cands("secret"),
        ROOM,
        "secret data is never sent",
    ),
    (
        "allow-cloud-false-local-down",
        "personal",
        LOCAL_ONLY,
        cands("personal", local=False),
        ROOM,
        "no provider: local unavailable; cloud not allowed for this request",
    ),
    (
        "both-down",
        "personal",
        CLOUD_OK,
        cands("personal", local=False, cloud=False),
        ROOM,
        "no provider: local unavailable; cloud unavailable",
    ),
    (
        "sensitive-local-down",
        "sensitive",
        CLOUD_OK,
        cands("sensitive", local=False),
        ROOM,
        "local unavailable; cloud may not receive sensitive data",
    ),
    (
        "no-cloud-configured",
        "personal",
        CLOUD_OK,
        cands("personal", local=False, cloud=None),
        ROOM,
        "no cloud provider configured",
    ),
    (
        "override-cloud-without-allow-cloud",
        "personal",
        RouteOptions(override="claude"),
        cands("personal"),
        ROOM,
        "override 'claude' is cloud, but this request does not allow cloud",
    ),
    (
        "override-cloud-sensitive",
        "sensitive",
        RouteOptions(allow_cloud=True, override="claude"),
        cands("sensitive"),
        ROOM,
        "override 'claude' may not receive sensitive data",
    ),
    (
        "override-unavailable-no-substitute",
        "personal",
        RouteOptions(allow_cloud=True, override="ollama"),
        cands("personal", local=False),
        ROOM,
        "override 'ollama' is unavailable",
    ),
    (
        "override-unknown",
        "personal",
        RouteOptions(allow_cloud=True, override="gemini"),
        cands("personal"),
        ROOM,
        "override 'gemini' is not a configured provider",
    ),
]


@pytest.mark.parametrize(
    ("privacy", "options", "candidates", "headroom", "reason"),
    [pytest.param(*row[1:], id=row[0]) for row in REFUSALS],
)
def test_refusals(
    privacy: PrivacyLevel,
    options: RouteOptions,
    candidates: list[Candidate],
    headroom: Headroom | None,
    reason: str,
) -> None:
    with pytest.raises(NoRoute) as info:
        choose_route(privacy, options, candidates, headroom)
    assert reason in info.value.reason


# --- invariants over every combination ---------------------------------------------

AVAIL = (True, False, None)
HEADROOMS = (ROOM, NO_ROOM, UNKNOWN, None)
OVERRIDES = (None, "ollama", "claude")


def every_case() -> list[tuple[PrivacyLevel, RouteOptions, list[Candidate], Headroom | None]]:
    out = []
    for privacy, allow_cloud, override, local, cloud, headroom in itertools.product(
        PRIVACY_ORDER, (False, True), OVERRIDES, AVAIL, AVAIL, HEADROOMS
    ):
        options = RouteOptions(allow_cloud=allow_cloud, override=override)
        out.append((privacy, options, cands(privacy, local=local, cloud=cloud), headroom))
    return out


def outcome(
    privacy: PrivacyLevel,
    options: RouteOptions,
    candidates: list[Candidate],
    headroom: Headroom | None,
) -> ProviderKind | None:
    try:
        return choose_route(privacy, options, candidates, headroom).kind
    except NoRoute as exc:
        assert exc.reason  # every refusal explains itself
        return None


def test_every_combination_is_checked() -> None:
    assert len(every_case()) == 4 * 2 * 3 * 3 * 3 * 4


def test_cloud_never_chosen_without_allow_cloud() -> None:
    for case in every_case():
        if not case[1].allow_cloud:
            assert outcome(*case) != "cloud", case


def test_sensitive_never_goes_to_cloud() -> None:
    for case in every_case():
        if case[0] == "sensitive":
            assert outcome(*case) != "cloud", case


def test_secret_never_routed() -> None:
    for case in every_case():
        if case[0] == "secret":
            assert outcome(*case) is None, case


def test_override_never_substituted() -> None:
    for case in every_case():
        options = case[1]
        if options.override is not None:
            try:
                decision = choose_route(*case)
            except NoRoute:
                continue
            assert decision.provider == options.override, case


def test_every_decision_has_reason() -> None:
    for case in every_case():
        try:
            assert choose_route(*case).reason
        except NoRoute as exc:
            assert exc.reason


def test_override_name_validated() -> None:
    with pytest.raises(ValidationError):
        RouteOptions(override="Bad Name!")
    with pytest.raises(ValidationError):
        RouteOptions(override="x" * 33)
    with pytest.raises(ValidationError):
        RouteOptions(allow_cloud=True, extra_field=1)  # type: ignore[call-arg]


def test_allow_cloud_defaults_false() -> None:
    assert RouteOptions().allow_cloud is False


# --- Router: probing, availability, calls ------------------------------------------


class FakeProvider(Provider):
    def __init__(
        self,
        name: str,
        kind: ProviderKind,
        *,
        available: bool = True,
        fail: bool = False,
        raise_on_check: bool = False,
    ) -> None:
        super().__init__(
            ProviderInfo(
                name=name,
                kind=kind,
                default_model=f"{name}-model",
                max_privacy="sensitive" if kind == "local" else "personal",
            )
        )
        self.available = available
        self.fail = fail
        self.raise_on_check = raise_on_check
        self.calls: list[ModelRequest] = []
        self.checks = 0

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self.fail:
            raise ProviderUnavailable(self.name, "down")
        return ModelResponse(
            text="ok", provider=self.name, model=self.info.default_model, kind=self.info.kind
        )

    async def check_available(self) -> bool:
        self.checks += 1
        if self.raise_on_check:
            raise RuntimeError("bug")
        return self.available


class Probe:
    def __init__(self, free_gib: float | None = 4.0, *, fail: bool = False) -> None:
        self.free_gib = free_gib
        self.fail = fail
        self.threads: list[threading.Thread] = []

    def __call__(self) -> HardwareSnapshot:
        self.threads.append(threading.current_thread())
        if self.fail:
            raise RuntimeError("probe bug")
        return snapshot(self.free_gib)


def req(privacy: PrivacyLevel = "personal") -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=PROMPT_TEXT),), privacy=privacy)


def make_router(
    identity: Identity, probe: Callable[[], HardwareSnapshot], **kw: FakeProvider
) -> tuple[Router, FakeProvider, FakeProvider]:
    local = kw.get("local") or FakeProvider("ollama", "local")
    cloud = kw.get("cloud") or FakeProvider("claude", "cloud")
    return Router([local, cloud], identity, secret_check=CHECK, probe=probe), local, cloud


def test_handle_calls_local_with_identity(
    identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aether.router")
    probe = Probe(4.0)
    router, local, cloud = make_router(identity, probe)

    result = asyncio.run(router.handle(req(), RouteOptions(allow_cloud=True)))

    assert result.decision.provider == "ollama"
    assert result.response.provider == "ollama"
    assert len(local.calls) == 1 and cloud.calls == []
    # Went through call_with_identity: identity system prompt applied.
    assert local.calls[0].system is not None
    assert local.calls[0].system.startswith(f"You are {identity.name}.")

    messages = [r.getMessage() for r in caplog.records]
    assert "Route chosen" in messages and "Model call" in messages
    chosen = next(r for r in caplog.records if r.getMessage() == "Route chosen")
    assert chosen.reason.startswith("local preferred")  # type: ignore[attr-defined]
    assert chosen.allow_cloud is True  # type: ignore[attr-defined]
    assert all(PROMPT_TEXT not in str(r.__dict__) for r in caplog.records)


def test_probe_runs_off_event_loop_thread(identity: Identity) -> None:
    probe = Probe(4.0)
    router, _, _ = make_router(identity, probe)

    asyncio.run(router.decide(req(), RouteOptions()))

    assert len(probe.threads) == 1
    assert probe.threads[0] is not threading.main_thread()


def test_low_vram_goes_cloud_when_allowed(identity: Identity) -> None:
    router, local, cloud = make_router(identity, Probe(0.5))

    result = asyncio.run(router.handle(req(), RouteOptions(allow_cloud=True)))

    assert result.decision.provider == "claude"
    assert local.calls == [] and len(cloud.calls) == 1


def test_allow_cloud_false_never_touches_cloud(identity: Identity) -> None:
    router, local, cloud = make_router(
        identity, Probe(0.5), local=FakeProvider("ollama", "local", fail=True)
    )

    with pytest.raises(RouteFailed) as info:
        asyncio.run(router.handle(req(), RouteOptions()))

    assert [a.error_type for a in info.value.attempts] == ["ProviderUnavailable"]

    assert len(local.calls) == 1
    assert cloud.calls == []
    assert cloud.checks == 0  # not even an availability check


def test_sensitive_never_reaches_cloud(identity: Identity) -> None:
    router, local, cloud = make_router(
        identity, Probe(0.5), local=FakeProvider("ollama", "local", available=False)
    )

    with pytest.raises(NoRoute, match="cloud may not receive sensitive data"):
        asyncio.run(router.handle(req("sensitive"), RouteOptions(allow_cloud=True)))

    assert local.calls == [] and cloud.calls == [] and cloud.checks == 0


def test_secret_refused_before_any_work(
    identity: Identity, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aether.router")
    probe = Probe(4.0)
    router, local, cloud = make_router(identity, probe)

    with pytest.raises(NoRoute, match="secret data is never sent"):
        asyncio.run(router.handle(req("secret"), RouteOptions(allow_cloud=True, override="ollama")))

    assert probe.threads == []
    assert local.checks == cloud.checks == 0
    assert local.calls == cloud.calls == []
    assert [r.getMessage() for r in caplog.records] == ["No route"]


def test_probe_failure_counts_as_unknown(identity: Identity) -> None:
    router, _, _ = make_router(identity, Probe(fail=True))

    decision = asyncio.run(router.decide(req(), RouteOptions(allow_cloud=True)))

    assert decision.provider == "claude"
    assert "GPU not probed" in decision.reason


def test_unknown_gpu_with_allow_cloud_false_stays_local(identity: Identity) -> None:
    router, _, _ = make_router(identity, Probe(None))

    decision = asyncio.run(router.decide(req(), RouteOptions()))

    assert decision.provider == "ollama"
    assert "GPU state unknown" in decision.reason


def test_check_available_raising_counts_as_unavailable(identity: Identity) -> None:
    router, _, _ = make_router(
        identity, Probe(4.0), local=FakeProvider("ollama", "local", raise_on_check=True)
    )

    decision = asyncio.run(router.decide(req(), RouteOptions(allow_cloud=True)))

    assert decision.provider == "claude"
    assert "local unavailable" in decision.reason


def test_override_cloud_needs_allow_cloud(identity: Identity) -> None:
    router, local, cloud = make_router(identity, Probe(4.0))

    with pytest.raises(NoRoute, match="does not allow cloud"):
        asyncio.run(router.handle(req(), RouteOptions(override="claude")))

    assert cloud.calls == [] and cloud.checks == 0 and local.calls == []


def test_duplicate_provider_names_rejected(identity: Identity) -> None:
    with pytest.raises(ValueError, match="unique"):
        Router(
            [FakeProvider("x", "local"), FakeProvider("x", "cloud")], identity, secret_check=CHECK
        )


def test_from_settings_builds_local_then_cloud(identity: Identity) -> None:
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("not-a-real-key"))
    router = Router.from_settings(settings, identity)
    assert [(p.name, p.info.kind) for p in router._providers] == [
        ("ollama", "local"),
        ("claude", "cloud"),
    ]
