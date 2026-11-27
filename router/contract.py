"""Provider-neutral model contract (docs/03 section 4).

Core and routing code speak only these types; vendor SDK types stay inside the
adapters. Every call goes through ``Provider.generate()``, which applies the same
guards to every adapter:

- privacy: a request above the provider's allowed level is refused before any
  network call, and ``secret`` is never sent to any provider (docs/05 §7.1);
- timeout: ``request.timeout_s`` is enforced;
- errors: anything an adapter raises that is not a ``ProviderError`` becomes
  ``ProviderBadResponse`` *without* copying its message, so vendor exception
  text (which may echo request details) never leaves the adapter unchecked.

Adapters implement ``_generate()`` and ``check_available()`` only.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Annotated, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# Provider names are short identifiers, safe to echo in reasons, logs and events.
# The one definition (#33); the events table's CHECK constraint keeps a frozen copy.
PROVIDER_NAME_PATTERN = r"[a-z0-9_-]{1,32}"
# Memory size limits per call (ADR-024 D9). Defined here, the base of the router,
# so ProviderInfo can bound a provider's own lower limits; memory/retrieval.py uses
# the same values (MAX_RECORDS, MAX_CHARS).
MEMORY_MAX_RECORDS = 20
MEMORY_MAX_CHARS = 4000  # rendered lines, without the fixed sentence
ProviderName = Annotated[str, StringConstraints(pattern=rf"^{PROVIDER_NAME_PATTERN}$")]

# The one place the privacy level names are listed (docs/05 §7.1, #58), lowest first.
# Everything else derives from it: the order, the ranks, and the non-secret subset.
PrivacyLevel = Literal["public", "personal", "sensitive", "secret"]
PRIVACY_ORDER: tuple[PrivacyLevel, ...] = get_args(PrivacyLevel)
# Secret is never sent to a model, never included by default, never a provider's maximum.
SECRET: PrivacyLevel = PRIVACY_ORDER[-1]
NON_SECRET_LEVELS: tuple[PrivacyLevel, ...] = PRIVACY_ORDER[:-1]


def _not_secret(level: PrivacyLevel) -> PrivacyLevel:
    if level == SECRET:
        raise ValueError(f"'{SECRET}' is not allowed here (docs/05 §7.1)")
    return level


# A privacy level other than secret, checked at validation time. Use it in pydantic
# models (identity policy, provider config); the static type is still PrivacyLevel.
NonSecretLevel = Annotated[PrivacyLevel, AfterValidator(_not_secret)]

ProviderKind = Literal["local", "cloud"]
Role = Literal["user", "assistant"]
# "refusal": the model declined (e.g. a safety classifier); the response has no text.
FinishReason = Literal["stop", "length", "refusal", "other"]

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


def privacy_rank(level: PrivacyLevel) -> int:
    return PRIVACY_ORDER.index(level)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(_Frozen):
    role: Role
    content: NonEmptyStr


class ModelRequest(_Frozen):
    """One text generation request. The system prompt (identity, #16) is separate."""

    messages: Annotated[tuple[Message, ...], Field(min_length=1)]
    system: str | None = None
    model: NonEmptyStr | None = None  # None: provider's default model
    max_tokens: Annotated[int, Field(gt=0)] | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    timeout_s: Annotated[float, Field(gt=0.0, le=600.0)] = 60.0
    # Highest privacy level present in the request. Unclassified = personal (docs/05).
    privacy: PrivacyLevel = "personal"


class Usage(_Frozen):
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None


class ModelResponse(_Frozen):
    text: str
    provider: NonEmptyStr
    model: NonEmptyStr
    kind: ProviderKind
    finish_reason: FinishReason = "stop"
    usage: Usage | None = None
    latency_ms: Annotated[float, Field(ge=0.0)] = 0.0  # filled in by Provider.generate()


class ProviderInfo(_Frozen):
    name: NonEmptyStr
    kind: ProviderKind
    default_model: NonEmptyStr
    # Highest privacy level this provider may receive. Never "secret".
    max_privacy: NonSecretLevel
    # Highest level of *memory* this provider may get in a call (ADR-024 §3, #139).
    # Set by each adapter from facts it has when it is built (host trust for Ollama),
    # so selection never guesses trust from kind and max_privacy. None (the default):
    # no memory at all, so a provider that doesn't set it fails closed.
    memory_limit: NonSecretLevel | None = None
    # Lower memory size limits for this provider (ADR-024 D9, #143), e.g. a small
    # local model with a short context. None: the defaults (MEMORY_MAX_RECORDS,
    # MEMORY_MAX_CHARS). Never above them.
    memory_max_records: Annotated[int, Field(ge=1, le=MEMORY_MAX_RECORDS)] | None = None
    memory_max_chars: Annotated[int, Field(ge=1, le=MEMORY_MAX_CHARS)] | None = None
    # A local provider whose data leaves this machine and network: an Ollama host
    # that is not trusted (ADR-023) and not trusted by the owner's opt-in. Treated
    # like the cloud for privacy, not for routing order (ADR-032): it needs
    # allow_cloud, gets memory only with it, and gets the #8 tripwire.
    off_network: bool = False

    @property
    def needs_allow_cloud(self) -> bool:
        """Whether a request must set ``allow_cloud`` to reach this provider. Anything
        not explicitly local and on-network does, so a new kind fails closed."""
        return self.kind != "local" or self.off_network

    @model_validator(mode="after")
    def _off_network_is_a_capped_local_provider(self) -> "ProviderInfo":
        if self.off_network and (
            self.kind != "local" or privacy_rank(self.max_privacy) > privacy_rank("personal")
        ):
            raise ValueError("off_network is for a local provider capped at personal")
        return self

    @model_validator(mode="after")
    def _cloud_never_gets_sensitive_by_default(self) -> "ProviderInfo":
        # docs/05 §7.1: Sensitive is local-only unless the owner approves a specific
        # request; per-request approval is a routing concern, not provider config.
        if self.kind == "cloud" and self.max_privacy == "sensitive":
            raise ValueError("cloud providers may not be configured to accept sensitive")
        return self

    @model_validator(mode="after")
    def _memory_limit_within_max_privacy(self) -> "ProviderInfo":
        if self.memory_limit is not None and privacy_rank(self.memory_limit) > privacy_rank(
            self.max_privacy
        ):
            raise ValueError("memory_limit may not be above max_privacy")
        return self

    def memory_limit_for(self, *, allow_cloud: bool) -> PrivacyLevel | None:
        """The memory limit for one call (ADR-024 §3): ``None`` means no memory.

        A provider that isn't explicitly local, or is off-network (ADR-032), is
        treated as cloud, so a new kind fails closed: it gets no memory unless the
        request has ``allow_cloud``."""
        if self.needs_allow_cloud and not allow_cloud:
            return None
        return self.memory_limit


# --- errors ----------------------------------------------------------------------


class ProviderError(Exception):
    """Base for every error a provider call can raise.

    ``retryable`` means retrying the *same* provider later may succeed. Whether to
    fail over to another provider is decided by routing (#19), not here.
    Messages must never contain secrets; adapters build them from safe parts only.
    """

    default_retryable = False

    def __init__(self, provider: str, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.retryable = self.default_retryable if retryable is None else retryable


class ProviderTimeout(ProviderError):
    default_retryable = True


class ProviderUnavailable(ProviderError):
    """Not reachable or not configured (e.g. connection refused, no API key)."""

    default_retryable = True


class ProviderBadResponse(ProviderError):
    """The provider answered, but not with something usable; or an adapter bug."""


class PrivacyNotAllowed(ProviderError):
    """The request's privacy level is above what this provider may receive."""


class MemoryNotAllowed(PrivacyNotAllowed):
    """A record selected for this call may not go to this provider (ADR-024 §3, the
    ``call_with_identity`` backstop). Raised before anything is sent; never
    retryable. The message is fixed: no record, level or ID."""

    MESSAGE = "memory not allowed for this call; nothing sent"

    def __init__(self, provider: str) -> None:
        super().__init__(provider, self.MESSAGE, retryable=False)


class SecretContentRefused(ProviderError):
    """The text of a cloud send looks like a secret (#8). Raised before anything is
    sent; never retryable, so routing never fails over with it. The message is fixed
    and never says which text, pattern or value matched."""

    MESSAGE = "request text looks like a secret; not sent to a provider off this network"

    def __init__(self, provider: str) -> None:
        super().__init__(provider, self.MESSAGE, retryable=False)


# --- provider base class -------------------------------------------------------


class Provider(ABC):
    def __init__(self, info: ProviderInfo) -> None:
        self.info = info

    @property
    def name(self) -> str:
        return self.info.name

    def allows(self, privacy: PrivacyLevel) -> bool:
        return privacy != SECRET and privacy_rank(privacy) <= privacy_rank(self.info.max_privacy)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not self.allows(request.privacy):
            raise PrivacyNotAllowed(
                self.name,
                f"request privacy '{request.privacy}' exceeds '{self.info.max_privacy}'",
            )

        started = time.perf_counter()
        try:
            async with asyncio.timeout(request.timeout_s):
                response = await self._generate(request)
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderTimeout(self.name, f"no response within {request.timeout_s}s") from exc
        except Exception as exc:
            # Type name only: the exception text may echo request or credential data.
            raise ProviderBadResponse(
                self.name, f"unexpected {type(exc).__name__} from adapter"
            ) from exc

        if response.provider != self.name or response.kind != self.info.kind:
            raise ProviderBadResponse(self.name, "adapter returned mismatched provider info")
        latency_ms = (time.perf_counter() - started) * 1000
        return response.model_copy(update={"latency_ms": latency_ms})

    @abstractmethod
    async def _generate(self, request: ModelRequest) -> ModelResponse:
        """Vendor call. Raise ProviderError subclasses for known failures."""

    @abstractmethod
    async def check_available(self) -> bool:
        """Cheap availability probe used by routing. Must not raise."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release network resources (HTTP connection pools). Called on shutdown."""
