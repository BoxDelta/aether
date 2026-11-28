"""llama-swap adapter (local inference) implementing the Nexus contract (ADR-040).

Calls llama-swap's OpenAI-compatible ``/v1/chat/completions`` directly. Never through
LiteLLM or Langfuse: they log full prompts, and Aether's context can be Personal or
Sensitive (docs/05 §7.1, §9). A base URL on LiteLLM's usual port gets one fixed-text
startup warning.

Rules this adapter keeps (each has a test):

- Thinking is off on every request (``chat_template_kwargs.enable_thinking=false``);
  a reply with empty content is ``ProviderBadResponse``, never an empty answer.
- Host trust is the Ollama rule (ADR-023, ADR-032), from the URL text alone: a
  loopback or private host is trusted (up to ``sensitive``); any other host is
  off-network, capped at ``personal``, and needs ``allow_cloud``, so the content
  tripwire runs. There is no trust opt-in for this adapter.
- The client follows no redirects and ignores proxy variables, so the API key and
  the prompt only go to the configured host.
- One request at a time (the model is shared with the dispatcher, docs/03 §3).
- ``check_available`` lists models (``GET /v1/models``), which never loads the model.
- Error messages are built from safe parts only (status code, model name); the
  server's own error text is never copied because it can echo request content. Nothing
  here logs a URL, host, key, prompt or reply.
"""

import asyncio
import logging
from typing import Any

import httpx
from pydantic import SecretStr

from core.config import ConfigError, Settings
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
from router.host_trust import TRUSTED_CATEGORIES, HostCategory, classify_host

logger = logging.getLogger("aether.router.llama_swap")

PROVIDER_NAME = "llama-swap"

# Fail fast when llama-swap isn't running; the overall deadline is request.timeout_s.
# A cold model takes about 25 s to load (ADR-040), which counts toward the read time.
CONNECT_TIMEOUT_S = 5.0
AVAILABILITY_TIMEOUT_S = 2.0

# LiteLLM's default port. Pointing the URL at it would put prompts in its logs.
LITELLM_PORTS = frozenset({4000})

_FINISH_REASONS: dict[str, FinishReason] = {"stop": "stop", "length": "length"}

URL_ERROR = "LLAMA_SWAP_URL must be an http(s) URL without a username or password"


def _origin(url: str) -> str:
    """``scheme://host:port`` of the configured URL, nothing else. A path, query,
    fragment or credentials in the setting are dropped or refused, so only the
    host that was checked for trust is ever contacted."""
    parsed = httpx.URL(url)
    if parsed.userinfo:
        raise ConfigError(URL_ERROR)  # never echo the value
    return f"{parsed.scheme}://{parsed.netloc.decode('ascii')}"


class LlamaSwapProvider(Provider):
    def __init__(  # noqa: PLR0913 - keyword-only settings
        self,
        url: str,
        default_model: str,
        *,
        api_key: SecretStr | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        memory_max_records: int | None = None,
        memory_max_chars: int | None = None,
    ) -> None:
        category = classify_host(url)
        trusted = category in TRUSTED_CATEGORIES
        origin = _origin(url)
        port = httpx.URL(url).port
        super().__init__(
            ProviderInfo(
                name=PROVIDER_NAME,
                kind="local",
                default_model=default_model,
                # Sensitive context only on a trusted host; never Secret (docs/05 §7.1).
                max_privacy="sensitive" if trusted else "personal",
                memory_limit="sensitive" if trusted else "personal",
                # Any other host is off-network (ADR-032): like the cloud, it needs
                # allow_cloud for requests and memory. No opt-in (ADR-040).
                off_network=not trusted,
                memory_max_records=memory_max_records,
                memory_max_chars=memory_max_chars,
            )
        )
        self.host_category: HostCategory = category
        if not trusted:
            # Fixed text only: no variable reaches the log call, so the host can't leak.
            logger.warning(
                "LLAMA_SWAP_URL is not loopback or a private address; llama-swap is treated "
                "like the cloud: it takes only requests with allow_cloud, at most personal.",
            )
        if port in LITELLM_PORTS:
            logger.warning(
                "LLAMA_SWAP_URL uses a port LiteLLM commonly uses. Aether must talk to "
                "llama-swap directly: LiteLLM and Langfuse log full prompts.",
            )
        self._origin = origin
        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"} if api_key else {}
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(None, connect=CONNECT_TIMEOUT_S),
            # Host trust is checked on the configured URL only, so the client must not
            # go anywhere else: no redirects (the key and the body would follow them)
            # and no proxies from HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (ADR-023).
            "follow_redirects": False,
            "trust_env": False,
            "headers": headers,
        }
        if transport is not None:
            client_kwargs["transport"] = transport  # tests inject httpx.MockTransport
        self._client = httpx.AsyncClient(**client_kwargs)
        # One request at a time to begin with (ADR-040): the model is shared.
        self._slot = asyncio.Semaphore(1)

    @classmethod
    def from_settings(cls, settings: Settings) -> "LlamaSwapProvider":
        if settings.llama_swap_model is None:
            raise ConfigError("LLAMA_SWAP_MODEL is not set")
        return cls(
            url=str(settings.llama_swap_url),
            default_model=settings.llama_swap_model,
            api_key=settings.llama_swap_api_key,
        )

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        model = request.model or self.info.default_model
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        if request.system:
            messages.insert(0, {"role": "system", "content": request.system})
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            # Always off: with thinking on, replies come back empty (ADR-040).
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens

        async with self._slot:
            try:
                response = await self._client.post(f"{self._origin}/v1/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                raise ProviderTimeout(self.name, "llama-swap did not respond in time") from exc
            except httpx.TransportError as exc:
                raise ProviderUnavailable(
                    self.name, f"cannot reach llama-swap ({type(exc).__name__})"
                ) from exc
        if response.status_code != httpx.codes.OK:
            raise _map_status(response.status_code, model)
        return self._to_response(response, model)

    def _to_response(self, response: httpx.Response, requested_model: str) -> ModelResponse:
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderBadResponse(self.name, "response is not a chat completion") from exc
        if not isinstance(content, str) or not content.strip():
            # Empty content is what a thinking model returns when thinking was not off.
            raise ProviderBadResponse(self.name, "response has empty content")
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        reason = choice.get("finish_reason")
        served = data.get("model")
        return ModelResponse(
            text=content,
            provider=self.name,
            model=served if isinstance(served, str) and served else requested_model,
            kind="local",
            finish_reason=_FINISH_REASONS.get(reason, "other")
            if isinstance(reason, str)
            else "other",
            usage=Usage(
                input_tokens=_count(usage.get("prompt_tokens")),
                output_tokens=_count(usage.get("completion_tokens")),
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_available(self) -> bool:
        """True if llama-swap answers and lists the default model. Never raises.

        Lists models only (``GET /v1/models``); that never loads the model, so a
        readiness or routing check can't wake the GPU (ADR-040)."""
        try:
            async with asyncio.timeout(AVAILABILITY_TIMEOUT_S):
                response = await self._client.get(f"{self._origin}/v1/models")
            if response.status_code != httpx.codes.OK:
                logger.debug(
                    "llama-swap model list refused", extra={"status": response.status_code}
                )
                return False
            ids = {m.get("id") for m in response.json()["data"] if isinstance(m, dict)}
        except Exception as exc:
            logger.debug("llama-swap unavailable", extra={"reason": type(exc).__name__})
            return False
        present = self.info.default_model in ids
        if not present:
            logger.debug("llama-swap does not list the default model")
        return present


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _map_status(status: int, model: str) -> ProviderUnavailable | ProviderBadResponse:
    if status in (401, 403):
        return ProviderUnavailable(
            PROVIDER_NAME, f"llama-swap refused the API key (HTTP {status})", retryable=False
        )
    if status == 404:
        return ProviderUnavailable(
            PROVIDER_NAME, f"model '{model}' not found on llama-swap", retryable=False
        )
    if status >= 500:
        return ProviderUnavailable(PROVIDER_NAME, f"llama-swap server error (HTTP {status})")
    # Includes a redirect (3xx), which is never followed.
    return ProviderBadResponse(PROVIDER_NAME, f"llama-swap rejected the request (HTTP {status})")
