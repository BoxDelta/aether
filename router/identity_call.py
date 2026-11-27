"""Apply the AETHER identity to a model call and log the call (docs/07 §4, #16).

Every model call goes through :func:`call_with_identity`, whichever provider
answers: it replaces ``request.system`` with the identity prompt (the caller's
``system`` becomes the task-instructions section) and logs one ``Model call``
record with the identity version. Routing (#18) and failover (#19) call providers
only through this function.

The log record carries metadata only (provider, model, privacy level, outcome,
identity version); never prompt or response text.

**Content tripwire for cloud sends (#8).** Before a cloud provider is called, every
text that would be sent (each message and the full system prompt) is checked with
``SecretCheck`` (``core/log.py``: the log redactor's patterns and the configured
secret values). On a match the call is refused with ``SecretContentRefused``
(not retryable, so no failover) and nothing is sent, whatever the privacy level
says. Local providers are not checked (an untrusted ``OLLAMA_HOST`` is still
``local`` until #22 decides otherwise). Being here, in the one call path, the
check also covers failover. ``_outgoing_texts`` lists the request's text fields;
tests/test_cloud_tripwire.py pins ``ModelRequest``'s fields, so a new one must be
added here or the test fails. Memory goes into the system prompt, so the tripwire
checks memory text as well.

**Memory (ADR-024 §3, step 12 of #58).** ``memory`` is the ``MemoryContext`` the
router selected for this call. It goes only into the ``memory_context`` section of
the system prompt, and the request sent carries the effective privacy level (the
higher of the request's and its memory's). Before anything is sent, ``check_memory``
checks every record's level against this provider's own memory limit,
``allow_cloud`` and ``include_sensitive`` (``MemoryNotAllowed`` if one doesn't
fit); ``Provider.generate()`` then checks the effective level against
``max_privacy``.

**Echo backstop.** A ``ProviderError`` raised here has its message replaced if it
contains text sent in this call (``router/echo.py``), before it is logged or
raised, so the ``Model call failed`` and failover logs, the ``/chat`` error body and
the events never carry it.
"""

import logging
import time

from core.identity import Identity, render_system_prompt
from core.log import SecretCheck
from router.contract import (
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    SecretContentRefused,
)
from router.echo import echo_free, long_enough, request_texts
from router.memory_context import MemoryContext, check_memory, effective_privacy

logger = logging.getLogger("aether.router.call")


def apply_identity(
    request: ModelRequest,
    identity: Identity,
    provider: str,
    memory: MemoryContext | None = None,
) -> ModelRequest:
    """Return a copy of ``request`` whose system prompt starts with the identity,
    with ``memory`` in its own section and the effective privacy level."""
    system = render_system_prompt(
        identity,
        provider=provider,
        memory_context=memory.context if memory is not None else None,
        task=request.system,
    )
    return request.model_copy(
        update={"system": system, "privacy": effective_privacy(request.privacy, memory)}
    )


def _outgoing_texts(request: ModelRequest) -> list[str]:
    texts = [m.content for m in request.messages]
    if request.system is not None:
        texts.append(request.system)
    return texts


async def call_with_identity(  # noqa: PLR0913 - keyword-only options
    provider: Provider,
    request: ModelRequest,
    identity: Identity,
    *,
    secret_check: SecretCheck,
    memory: MemoryContext | None = None,
    allow_cloud: bool = False,
    include_sensitive: bool = False,
) -> ModelResponse:
    """Call ``provider`` with the identity and ``memory`` applied. ``secret_check``
    is required: callers build it with ``SecretCheck.from_settings`` so configured
    values count. ``allow_cloud`` and ``include_sensitive`` are the request's own
    options, for the memory backstop; both default to the safe value."""
    if not isinstance(secret_check, SecretCheck):
        raise TypeError("secret_check must be a core.log.SecretCheck")
    prepared = apply_identity(request, identity, provider.name, memory)
    forbidden = request_texts(request) + long_enough(memory.texts if memory else ())
    fields: dict[str, object] = {
        "identity_name": identity.name,
        "identity_version": identity.version,
        "provider": provider.name,
        "provider_kind": provider.info.kind,
        "model": request.model or provider.info.default_model,
        "privacy": request.privacy,
        "effective_privacy": prepared.privacy,
        "memory_records": len(memory.record_ids) if memory is not None else 0,
    }
    started = time.perf_counter()
    try:
        if memory is not None and memory.record_ids:
            check_memory(
                provider.info,
                memory,
                allow_cloud=allow_cloud,
                include_sensitive=include_sensitive,
            )
        # Anything not explicitly local is checked, so a new provider kind fails closed.
        if provider.info.kind != "local" and any(map(secret_check, _outgoing_texts(prepared))):
            raise SecretContentRefused(provider.name)
        response = await provider.generate(prepared)
    except ProviderError as exc:
        # Every later use (logs, Attempt, the /chat error body, events) reads this.
        exc.message = echo_free(exc.message, forbidden)
        exc.args = (f"{exc.provider}: {exc.message}",)
        logger.warning(
            "Model call failed",
            extra={
                **fields,
                "outcome": "error",
                "error_type": type(exc).__name__,
                "error": exc.message,
                "retryable": exc.retryable,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        raise
    logger.info(
        "Model call",
        extra={
            **fields,
            "model": response.model,  # the model that answered (may be a fallback)
            "outcome": "ok",
            "finish_reason": response.finish_reason,
            "input_tokens": response.usage.input_tokens if response.usage else None,
            "output_tokens": response.usage.output_tokens if response.usage else None,
            "latency_ms": round(response.latency_ms, 1),
        },
    )
    return response
