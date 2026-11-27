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
Routing calls providers only through ``call_with_identity`` (#16), which refuses a
cloud send whose text looks like a secret (#8, ``SecretContentRefused``, never
retried or failed over). ``Router`` requires the ``SecretCheck`` for that.

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
  adapters build from safe parts only and ``call_with_identity`` echo-checks.

Memory (ADR-024 §3, step 12 of #58), in ``Router._call``: with a ``MemorySource``,
every call, the fallback included, gets its own selection, made after its route is
chosen, for the provider that will receive it, with that provider's
``memory_limit_for(allow_cloud=...)``. A failover from a trusted local host to the
cloud therefore drops Sensitive and Personal records. Routing itself uses the
request's own level; memory never changes the route. If the source fails or takes
longer than ``MEMORY_TIMEOUT_S``, the call goes ahead without memory (one
metadata-only warning). ``on_memory`` reports each
selection (``MemoryUse``) before the call, for the ``memory.context.retrieved``
event and the events echo check.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from core.config import Settings
from core.identity import Identity
from core.log import SecretCheck
from router import hardware
from router.claude import ClaudeProvider
from router.contract import (
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    Provider,
    ProviderError,
    ProviderKind,
    ProviderName,
)
from router.hardware import MIN_FREE_VRAM_BYTES, HardwareSnapshot, Headroom, has_local_headroom
from router.identity_call import call_with_identity
from router.memory_context import (
    MemoryContext,
    MemorySource,
    effective_privacy,
    fetch_level,
)
from router.ollama import OllamaProvider

logger = logging.getLogger("aether.router.routing")

# A memory read that takes longer than this is given up: the call goes ahead
# without memory (docs/05 §12), so a stalled query can't hold /chat.
MEMORY_TIMEOUT_S = 2.0


class RouteOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Cloud is never chosen unless the request sets this (#18).
    allow_cloud: bool = False
    # Provider name (e.g. "ollama", "claude"). Bypasses the headroom rule, never
    # the allow_cloud or privacy rules.
    override: ProviderName | None = None
    # Sensitive memory may be included in this request (docs/07 §6, ADR-024 §3).
    # Never changes the route; only what a call's memory may hold.
    include_sensitive: bool = False


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
class MemoryUse:
    """The memory selected for one call (``on_memory``); ``memory`` holds text."""

    provider: str
    memory: MemoryContext
    effective_privacy: PrivacyLevel


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
    def __init__(  # noqa: PLR0913 - keyword-only options
        self,
        providers: Sequence[Provider],
        identity: Identity,
        *,
        secret_check: SecretCheck,
        probe: Callable[[], HardwareSnapshot] = hardware.probe,
        min_free_vram_bytes: int = MIN_FREE_VRAM_BYTES,
        memory: MemorySource | None = None,
    ) -> None:
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ValueError("provider names must be unique")
        # Required and type-checked, like MemoryStore's (#75): no router without it.
        if not isinstance(secret_check, SecretCheck):
            raise TypeError("secret_check must be a core.log.SecretCheck")
        self._secret_check = secret_check
        self._providers = tuple(providers)
        self._by_name = {p.name: p for p in providers}
        self._identity = identity
        self._probe = probe
        self._min_free_vram_bytes = min_free_vram_bytes
        self._memory = memory

    @classmethod
    def from_settings(
        cls, settings: Settings, identity: Identity, *, memory: MemorySource | None = None
    ) -> "Router":
        # Local first: on equal footing, the local provider is preferred.
        return cls(
            [OllamaProvider.from_settings(settings), ClaudeProvider.from_settings(settings)],
            identity,
            secret_check=SecretCheck.from_settings(settings),
            memory=memory,
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
        on_attempt_failed: Callable[[Attempt], None] | None = None,
        on_memory: Callable[[MemoryUse], None] | None = None,
    ) -> RoutedResponse:
        """Decide, call through ``call_with_identity``, fail over at most once.

        ``on_decision`` (optional) is told about every route chosen, before the
        call: the first decision and, on failover, the fallback (used for events).

        ``on_attempt_failed`` (optional) is told about each failed call as soon as
        it fails, before any fallback is chosen (used for events).

        Both hooks only report: if one raises, routing goes on as if it had not been
        called, and one metadata-only warning is logged.

        Raises ``NoRoute`` if no provider may take the request (nothing was
        called), or ``RouteFailed`` if a call failed and no fallback answered.
        """
        decision = await self.decide(request, options)
        _report(on_decision, decision, "Decision callback failed")
        try:
            response = await self._call(decision, request, options, on_memory)
        except ProviderError as exc:
            first = Attempt.from_error(exc)
            _report(on_attempt_failed, first, "Attempt callback failed")
            # Raises RouteFailed when there is no fallback.
            fallback = await self._fallback_decision(request, options, first, exc)
        else:
            # Any response is final, including finish_reason="refusal".
            return RoutedResponse(decision, response)
        _report(on_decision, fallback, "Decision callback failed")
        return await self._call_fallback(
            fallback, request, options, first, on_attempt_failed, on_memory
        )

    async def _call(
        self,
        decision: RouteDecision,
        request: ModelRequest,
        options: RouteOptions,
        on_memory: Callable[[MemoryUse], None] | None,
    ) -> ModelResponse:
        provider = self._by_name[decision.provider]
        memory = await self._memory_for(provider, request, options)
        if memory is not None:
            use = MemoryUse(provider.name, memory, effective_privacy(request.privacy, memory))
            _report(on_memory, use, "Memory callback failed")
        return await call_with_identity(
            provider,
            request,
            self._identity,
            secret_check=self._secret_check,
            memory=memory,
            allow_cloud=options.allow_cloud,
            include_sensitive=options.include_sensitive,
        )

    async def _memory_for(
        self, provider: Provider, request: ModelRequest, options: RouteOptions
    ) -> MemoryContext | None:
        """This call's memory, or None if the provider may get none (no event)."""
        limit = provider.info.memory_limit_for(allow_cloud=options.allow_cloud)
        if self._memory is None or limit is None:
            return None
        try:
            async with asyncio.timeout(MEMORY_TIMEOUT_S):
                return await self._memory.context_for(
                    _search_text(request),
                    limit=fetch_level(limit, include_sensitive=options.include_sensitive),
                    include_sensitive=options.include_sensitive,
                )
        except Exception as exc:  # memory is optional; the call goes ahead without it
            logger.warning(
                "Memory retrieval failed",
                extra={"provider": provider.name, "error_type": type(exc).__name__},
            )
            return None

    async def _call_fallback(  # noqa: PLR0913 - internal; one value per hook
        self,
        fallback: RouteDecision,
        request: ModelRequest,
        options: RouteOptions,
        first: Attempt,
        on_attempt_failed: Callable[[Attempt], None] | None,
        on_memory: Callable[[MemoryUse], None] | None,
    ) -> RoutedResponse:
        """The one fallback call, with its own memory selection. Any failure ends
        routing: at most one fallback."""
        try:
            response = await self._call(fallback, request, options, on_memory)
        except ProviderError as exc:
            second = Attempt.from_error(exc)
            _report(on_attempt_failed, second, "Attempt callback failed")
            reason = f"fallback {second.provider} also failed ({second.error_type})"
            logger.error(
                "Failover failed",
                extra={**_attempt_fields(second), "failed_first": first.provider, "reason": reason},
            )
            # No further attempts, whatever the error: at most one fallback.
            raise RouteFailed(reason, [first, second]) from exc
        return RoutedResponse(fallback, response, failed_first=first)

    async def _fallback_decision(
        self, request: ModelRequest, options: RouteOptions, first: Attempt, cause: ProviderError
    ) -> RouteDecision:
        """The fallback route, chosen by ``decide()`` without the failed provider.

        Raises ``RouteFailed`` with the one failed attempt (logged as "No failover") if
        the error is not retryable, a manual override is set, or no other route exists.
        """
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
        # Raised outside the NoRoute handler: the chain is the provider error, as before.
        raise RouteFailed(reason, [first]) from cause


def _search_text(request: ModelRequest) -> str | None:
    """The text memory is matched against: the latest user message."""
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content
    return None


def _report[T: (RouteDecision, Attempt, MemoryUse)](
    callback: Callable[[T], None] | None, value: T, failure_message: str
) -> None:
    """Tell a reporting hook about a decision, a failed call or a memory selection
    (``on_decision`` / ``on_attempt_failed`` / ``on_memory``). A hook can never
    change or break routing."""
    if callback is None:
        return
    try:
        callback(value)
    except Exception as exc:  # a reporting hook must not affect routing
        # Metadata only: the provider and the hook's error type, never its message.
        logger.warning(
            failure_message,
            extra={"provider": value.provider, "callback_error_type": type(exc).__name__},
        )


def _attempt_fields(attempt: Attempt) -> dict[str, object]:
    # Scrubbed error text only (ProviderError.message); never prompt or answer text.
    return {
        "provider": attempt.provider,
        "error_type": attempt.error_type,
        "error": attempt.message,
        "retryable": attempt.retryable,
    }
