"""Nexus routing rules (#18): choose local or cloud, and say why.

Inputs: the request's privacy level, per-request ``RouteOptions`` (``allow_cloud``,
manual ``override``), provider availability and the GPU/RAM probe (#17).

Rules, in order:

1. ``secret`` is never routed anywhere (docs/05 §7.1).
2. A manual override picks that provider, but only if the request allows it
   (cloud needs ``allow_cloud``, the provider must accept the privacy level) and
   it is available. Otherwise the request is refused; another provider is never
   substituted silently.
3. Local, if it is available and has VRAM headroom.
4. Cloud, if ``allow_cloud`` is set, the privacy level allows it and it is
   available. ``allow_cloud`` defaults to false, so cloud is opt-in per request.
5. Local without headroom, when it is the only allowed choice (the request is
   not lost; the reason says the desktop reserve is not met).
6. Otherwise refused with ``NoRoute``.

Every decision carries a human-readable reason and is logged without prompt text.
Routing calls providers only through ``call_with_identity`` (#16).

Failover (#19), in ``Router.handle``:

- A response is final whatever its ``finish_reason``. A refusal is an answer, not
  an error, and never triggers failover (above all not cloud -> local).
- Only a ``ProviderError`` with ``retryable=True`` triggers failover; a
  non-retryable error (auth, model not found, rejected request, bad response,
  privacy) does not.
- A manual override never fails over.
- The fallback is chosen by ``decide()`` again with the failed provider
  excluded, so every rule above applies to it (``allow_cloud``, Sensitive,
  Secret, availability, headroom).
- At most one fallback call; its failure ends the request. No loops.
- Any failure after a call raises ``RouteFailed`` with every attempt; nothing is
  dropped silently. Error text comes from ``ProviderError.message``, which
  adapters build from safe parts only.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from core.config import Settings
from core.identity import Identity
from router import hardware
from router.claude import ClaudeProvider
from router.contract import (
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    Provider,
    ProviderError,
    ProviderKind,
)
from router.hardware import MIN_FREE_VRAM_BYTES, HardwareSnapshot, Headroom, has_local_headroom
from router.identity_call import call_with_identity
from router.ollama import OllamaProvider

logger = logging.getLogger("aether.router.routing")

# Provider names are short identifiers; the pattern keeps an override safe to echo
# in reasons and logs.
ProviderName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,32}$")]


class RouteOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Cloud is never chosen unless the request sets this (#18).
    allow_cloud: bool = False
    # Provider name (e.g. "ollama", "claude"). Bypasses the headroom rule, never
    # the allow_cloud or privacy rules.
    override: ProviderName | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    kind: ProviderKind
    allows_privacy: bool
    available: bool


@dataclass(frozen=True, slots=True)
class RouteDecision:
    provider: str
    kind: ProviderKind
    reason: str


@dataclass(frozen=True, slots=True)
class Attempt:
    """One failed provider call. ``message`` is the scrubbed ``ProviderError.message``."""

    provider: str
    error_type: str
    message: str
    retryable: bool

    @classmethod
    def from_error(cls, exc: ProviderError) -> "Attempt":
        return cls(exc.provider, type(exc).__name__, exc.message, exc.retryable)


@dataclass(frozen=True, slots=True)
class RoutedResponse:
    decision: RouteDecision
    response: ModelResponse
    # The failed first attempt when this response came from the fallback, else None.
    failed_first: Attempt | None = None


class NoRoute(Exception):
    """No allowed provider can take the request. ``reason`` is safe to show and log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RouteFailed(Exception):
    """A provider was called and the request could not be answered.

    ``attempts`` lists every failed call in order (one or two); ``reason`` says
    why routing stopped. Both are safe to show and log.
    """

    def __init__(self, reason: str, attempts: Sequence[Attempt]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = tuple(attempts)


NEVER_SENT_REASON = "secret data is never sent to any model"


def _room_text(headroom: Headroom | None) -> str:
    return headroom.reason if headroom is not None else "GPU not probed"


def _why_not(
    kind: ProviderKind,
    candidates: Sequence[Candidate],
    privacy: PrivacyLevel,
    options: RouteOptions,
) -> str:
    if kind == "cloud" and not options.allow_cloud:
        return "cloud not allowed for this request"
    of_kind = [c for c in candidates if c.kind == kind]
    if not of_kind:
        return f"no {kind} provider configured"
    if not any(c.allows_privacy for c in of_kind):
        return f"{kind} may not receive {privacy} data"
    return f"{kind} unavailable"


def _override_route(
    privacy: PrivacyLevel,
    options: RouteOptions,
    candidates: Sequence[Candidate],
    headroom: Headroom | None,
) -> RouteDecision:
    """Rule 2: the manual override, never substituted. Raises ``NoRoute``."""
    name = options.override
    match = next((c for c in candidates if c.name == name), None)
    if match is None:
        raise NoRoute(f"override '{name}' is not a configured provider")
    if match.kind == "cloud" and not options.allow_cloud:
        raise NoRoute(f"override '{name}' is cloud, but this request does not allow cloud")
    if not match.allows_privacy:
        raise NoRoute(f"override '{name}' may not receive {privacy} data")
    if not match.available:
        raise NoRoute(f"override '{name}' is unavailable")
    reason = f"manual override: {name}"
    if match.kind == "local" and not (headroom is not None and headroom.ok):
        reason += f" (desktop VRAM reserve not met: {_room_text(headroom)})"
    return RouteDecision(match.name, match.kind, reason)


def choose_route(
    privacy: PrivacyLevel,
    options: RouteOptions,
    candidates: Sequence[Candidate],
    headroom: Headroom | None,
) -> RouteDecision:
    """Pure routing rules (see module docstring). Raises ``NoRoute``."""
    if privacy == "secret":
        raise NoRoute(NEVER_SENT_REASON)
    has_room = headroom is not None and headroom.ok

    if options.override is not None:
        return _override_route(privacy, options, candidates, headroom)

    local = [c for c in candidates if c.kind == "local" and c.allows_privacy and c.available]
    cloud = [
        c
        for c in candidates
        if c.kind == "cloud" and options.allow_cloud and c.allows_privacy and c.available
    ]

    if local and has_room:
        return RouteDecision(local[0].name, "local", f"local preferred: {_room_text(headroom)}")
    if cloud:
        why_not_local = (
            f"no local VRAM headroom ({_room_text(headroom)})"
            if local
            else _why_not("local", candidates, privacy, options)
        )
        return RouteDecision(cloud[0].name, "cloud", f"cloud: {why_not_local}")
    if local:
        why_not_cloud = _why_not("cloud", candidates, privacy, options)
        return RouteDecision(
            local[0].name,
            "local",
            f"local without VRAM headroom ({_room_text(headroom)}); {why_not_cloud}",
        )
    raise NoRoute(
        f"no provider: {_why_not('local', candidates, privacy, options)}; "
        f"{_why_not('cloud', candidates, privacy, options)}"
    )


async def _available(provider: Provider) -> bool:
    try:
        return await provider.check_available()
    except Exception:  # contract says it must not raise; don't trust that blindly
        logger.warning("check_available raised", extra={"provider": provider.name})
        return False


class Router:
    def __init__(
        self,
        providers: Sequence[Provider],
        identity: Identity,
        *,
        probe: Callable[[], HardwareSnapshot] = hardware.probe,
        min_free_vram_bytes: int = MIN_FREE_VRAM_BYTES,
    ) -> None:
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError("provider names must be unique")
        self._providers = tuple(providers)
        self._by_name = {p.name: p for p in providers}
        self._identity = identity
        self._probe = probe
        self._min_free_vram_bytes = min_free_vram_bytes

    @classmethod
    def from_settings(cls, settings: Settings, identity: Identity) -> "Router":
        # Local first: on equal footing, the local provider is preferred.
        return cls(
            [OllamaProvider.from_settings(settings), ClaudeProvider.from_settings(settings)],
            identity,
        )

    def provider(self, name: str) -> Provider | None:
        """The configured provider with this name, if any (readiness checks)."""
        return self._by_name.get(name)

    async def aclose(self) -> None:
        """Close every provider's network client. Never raises; called on shutdown."""
        for provider in self._providers:
            try:
                await provider.aclose()
            except Exception as exc:
                logger.warning(
                    "Provider close failed",
                    extra={"provider": provider.name, "error_type": type(exc).__name__},
                )

    async def _headroom(self) -> Headroom | None:
        try:
            # The probe blocks (NVML / nvidia-smi subprocess); keep the event loop free.
            snapshot = await asyncio.to_thread(self._probe)
        except Exception as exc:  # probe must not raise; treat a failure as unknown
            logger.warning("Hardware probe failed", extra={"reason": type(exc).__name__})
            return None
        return has_local_headroom(snapshot, self._min_free_vram_bytes)

    async def decide(
        self,
        request: ModelRequest,
        options: RouteOptions,
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> RouteDecision:
        """Apply the routing rules. ``exclude`` names providers that already failed
        for this request (failover); they are treated as unavailable."""
        privacy = request.privacy
        log_fields: dict[str, object] = {
            "privacy": privacy,
            "allow_cloud": options.allow_cloud,
            "override": options.override,
        }
        if exclude:
            log_fields["excluded"] = sorted(exclude)
        try:
            if privacy == "secret":
                raise NoRoute(NEVER_SENT_REASON)  # before any probe or availability check

            # Only providers this request could use are checked.
            eligible = [
                p
                for p in self._providers
                if p.name not in exclude
                and p.allows(privacy)
                and (p.info.kind == "local" or options.allow_cloud)
            ]
            need_probe = any(p.info.kind == "local" for p in eligible)
            headroom_task = asyncio.ensure_future(self._headroom()) if need_probe else None
            statuses = await asyncio.gather(*(_available(p) for p in eligible))
            headroom = await headroom_task if headroom_task is not None else None

            available = {p.name: ok for p, ok in zip(eligible, statuses, strict=True)}
            candidates = [
                Candidate(
                    name=p.name,
                    kind=p.info.kind,
                    allows_privacy=p.allows(privacy),
                    available=available.get(p.name, False),
                )
                for p in self._providers
            ]
            decision = choose_route(privacy, options, candidates, headroom)
        except NoRoute as exc:
            logger.warning("No route", extra={**log_fields, "reason": exc.reason})
            raise
        logger.info(
            "Route chosen",
            extra={
                **log_fields,
                "provider": decision.provider,
                "provider_kind": decision.kind,
                "reason": decision.reason,
            },
        )
        return decision

    async def handle(
        self,
        request: ModelRequest,
        options: RouteOptions,
        *,
        on_decision: Callable[[RouteDecision], None] | None = None,
    ) -> RoutedResponse:
        """Decide, call through ``call_with_identity``, fail over at most once.

        ``on_decision`` (optional) is told about every route chosen, before the
        call: the first decision and, on failover, the fallback (used for events).

        Raises ``NoRoute`` if no provider may take the request (nothing was
        called), or ``RouteFailed`` if a call failed and no fallback answered.
        """
        decision = await self.decide(request, options)
        if on_decision is not None:
            on_decision(decision)
        try:
            response = await self._call(decision, request)
        except ProviderError as exc:
            first = Attempt.from_error(exc)
            fallback = await self._fallback_decision(request, options, first)
            if isinstance(fallback, str):
                raise RouteFailed(fallback, [first]) from exc
            if on_decision is not None:
                on_decision(fallback)
        else:
            # Any response is final, including finish_reason="refusal".
            return RoutedResponse(decision, response)

        try:
            response = await self._call(fallback, request)
        except ProviderError as exc:
            second = Attempt.from_error(exc)
            reason = f"fallback {second.provider} also failed ({second.error_type})"
            logger.error(
                "Failover failed",
                extra={**_attempt_fields(second), "failed_first": first.provider, "reason": reason},
            )
            # No further attempts, whatever the error: at most one fallback.
            raise RouteFailed(reason, [first, second]) from exc
        return RoutedResponse(fallback, response, failed_first=first)

    async def _call(self, decision: RouteDecision, request: ModelRequest) -> ModelResponse:
        provider = self._by_name[decision.provider]
        return await call_with_identity(provider, request, self._identity)

    async def _fallback_decision(
        self, request: ModelRequest, options: RouteOptions, first: Attempt
    ) -> RouteDecision | str:
        """The fallback route, or the reason there is none (logged as "No failover")."""
        if not first.retryable:
            reason = f"no failover: {first.provider} error is not retryable ({first.error_type})"
        elif options.override is not None:
            reason = f"no failover: manual override '{options.override}'"
        else:
            try:
                decision = await self.decide(request, options, exclude=frozenset({first.provider}))
            except NoRoute as exc:
                reason = f"no failover: {exc.reason}"
            else:
                logger.warning(
                    "Failover",
                    extra={
                        **_attempt_fields(first),
                        "to_provider": decision.provider,
                        "to_kind": decision.kind,
                        "reason": f"{first.provider} failed ({first.error_type}); "
                        f"{decision.reason}",
                    },
                )
                return decision
        logger.warning("No failover", extra={**_attempt_fields(first), "reason": reason})
        return reason


def _attempt_fields(attempt: Attempt) -> dict[str, object]:
    # Scrubbed error text only (ProviderError.message); never prompt or answer text.
    return {
        "provider": attempt.provider,
        "error_type": attempt.error_type,
        "error": attempt.message,
        "retryable": attempt.retryable,
    }
