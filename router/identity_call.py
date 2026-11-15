"""Apply the AETHER identity to a model call and log the call (docs/07 §4, #16).

Every model call goes through :func:`call_with_identity`, whichever provider
answers: it replaces ``request.system`` with the identity prompt (the caller's
``system`` becomes the task-instructions section) and logs one ``Model call``
record with the identity version. Routing (#18) and failover (#19) call providers
only through this function.

The log record carries metadata only (provider, model, privacy level, outcome,
identity version); never prompt or response text.
"""

import logging
import time

from core.identity import Identity, render_system_prompt
from router.contract import ModelRequest, ModelResponse, Provider, ProviderError

logger = logging.getLogger("aether.router.call")


def apply_identity(request: ModelRequest, identity: Identity, provider: str) -> ModelRequest:
    """Return a copy of ``request`` whose system prompt starts with the identity."""
    system = render_system_prompt(identity, provider=provider, task=request.system)
    return request.model_copy(update={"system": system})


async def call_with_identity(
    provider: Provider, request: ModelRequest, identity: Identity
) -> ModelResponse:
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
