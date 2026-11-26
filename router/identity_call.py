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
tests/test_cloud_tripwire.py pins ``ModelRequest``'s fields, so a new one (memory
context, step 12 of #58) must be added here or the test fails.
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

logger = logging.getLogger("aether.router.call")


def apply_identity(request: ModelRequest, identity: Identity, provider: str) -> ModelRequest:
    """Return a copy of ``request`` whose system prompt starts with the identity."""
    system = render_system_prompt(identity, provider=provider, task=request.system)
    return request.model_copy(update={"system": system})


def _outgoing_texts(request: ModelRequest) -> list[str]:
    texts = [m.content for m in request.messages]
    if request.system is not None:
        texts.append(request.system)
    return texts


async def call_with_identity(
    provider: Provider,
    request: ModelRequest,
    identity: Identity,
    *,
    secret_check: SecretCheck,
) -> ModelResponse:
    """Call ``provider`` with the identity applied. ``secret_check`` is required:
    callers build it with ``SecretCheck.from_settings`` so configured values count."""
    if not isinstance(secret_check, SecretCheck):
        raise TypeError("secret_check must be a core.log.SecretCheck")
    prepared = apply_identity(request, identity, provider.name)
    fields: dict[str, object] = {
        "identity_name": identity.name,
        "identity_version": identity.version,
        "provider": provider.name,
        "provider_kind": provider.info.kind,
        "model": request.model or provider.info.default_model,
        "privacy": request.privacy,
    }
    started = time.perf_counter()
    try:
        # Anything not explicitly local is checked, so a new provider kind fails closed.
        if provider.info.kind != "local" and any(map(secret_check, _outgoing_texts(prepared))):
            raise SecretContentRefused(provider.name)
        response = await provider.generate(prepared)
    except ProviderError as exc:
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
