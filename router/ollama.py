"""Ollama adapter (local inference) implementing the Nexus contract.

Translates ``ModelRequest`` to Ollama's chat API via the official ``ollama`` SDK
and maps every failure onto contract errors. Error messages are built only from
safe parts (status code, model name); Ollama's own error text is not copied
because it can echo request content.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from ollama import AsyncClient, ChatResponse, ResponseError

from core.config import Settings
from router.contract import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderBadResponse,
    ProviderInfo,
    ProviderTimeout,
    ProviderUnavailable,
    Usage,
)

logger = logging.getLogger("aether.router.ollama")

PROVIDER_NAME = "ollama"

# Fail fast when Ollama isn't running; the overall request deadline is enforced by
# Provider.generate() via request.timeout_s, so reads are not capped here.
CONNECT_TIMEOUT_S = 5.0
AVAILABILITY_TIMEOUT_S = 2.0

_FINISH_REASONS: dict[str, FinishReason] = {"stop": "stop", "length": "length"}


class OllamaProvider(Provider):
    def __init__(
        self,
        host: str,
        default_model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            ProviderInfo(
                name=PROVIDER_NAME,
                kind="local",
                default_model=default_model,
                # Local inference may receive Sensitive context; never Secret (docs/05 §7.1).
                max_privacy="sensitive",
            )
        )
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(None, connect=CONNECT_TIMEOUT_S),
        }
        if transport is not None:
            client_kwargs["transport"] = transport  # tests inject httpx.MockTransport
        self._client = AsyncClient(host=host, **client_kwargs)

    @classmethod
    def from_settings(cls, settings: Settings) -> "OllamaProvider":
        return cls(host=str(settings.ollama_host), default_model=settings.ollama_model)

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self.info.default_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        if request.system:
            messages.insert(0, {"role": "system", "content": request.system})
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens

        try:
            raw = await self._client.chat(
                model=model, messages=messages, options=options or None, stream=False
            )
        except ConnectionError as exc:
            raise ProviderUnavailable(self.name, "cannot connect to Ollama") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, "Ollama did not respond in time") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(self.name, f"transport error ({type(exc).__name__})") from exc
        except ResponseError as exc:
            raise _map_response_error(exc, model) from exc

        return self._to_response(raw, model)

    def _to_response(self, raw: ChatResponse, requested_model: str) -> ModelResponse:
        content = raw.message.content if raw.message is not None else None
        if content is None:
            raise ProviderBadResponse(self.name, "response has no message content")
        return ModelResponse(
            text=content,
            provider=self.name,
            model=raw.model or requested_model,
            kind="local",
            finish_reason=_FINISH_REASONS.get(raw.done_reason or "", "other"),
            usage=Usage(input_tokens=raw.prompt_eval_count, output_tokens=raw.eval_count),
        )

    async def aclose(self) -> None:
        # The ollama SDK leaves close() unannotated; state its signature here.
        close: Callable[[], Awaitable[None]] = self._client.close
        await close()

    async def check_available(self) -> bool:
        """True if Ollama answers and the default model is pulled. Never raises."""
        try:
            async with asyncio.timeout(AVAILABILITY_TIMEOUT_S):
                listing = await self._client.list()
        except Exception as exc:
            logger.debug("Ollama unavailable", extra={"reason": type(exc).__name__})
            return False
        names = {m.model for m in listing.models if m.model}
        wanted = self.info.default_model
        present = wanted in names or (":" not in wanted and f"{wanted}:latest" in names)
        if not present:
            logger.debug("Ollama default model not pulled", extra={"model": wanted})
        return present


def _map_response_error(
    exc: ResponseError, model: str
) -> ProviderUnavailable | ProviderBadResponse:
    status = exc.status_code
    if status == 404:
        return ProviderUnavailable(
            PROVIDER_NAME, f"model '{model}' not found (run: ollama pull {model})", retryable=False
        )
    if status >= 500:
        return ProviderUnavailable(PROVIDER_NAME, f"Ollama server error (HTTP {status})")
    return ProviderBadResponse(PROVIDER_NAME, f"Ollama rejected the request (HTTP {status})")
