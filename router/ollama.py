"""Ollama adapter (local inference) implementing the Nexus contract.

Translates ``ModelRequest`` to Ollama's chat API via the official ``ollama`` SDK
and maps every failure onto contract errors. Error messages are built only from
safe parts (status code, model name); Ollama's own error text is not copied
because it can echo request content.

Host trust (#10, ADR-023, docs/05 §7.1): Ollama may receive Sensitive context only
when ``OLLAMA_HOST`` is on this machine or the local network. The host is
classified from the URL text alone; DNS is never resolved. Trusted: ``localhost``
and literal IPs in 127.0.0.0/8, ::1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
and link-local (169.254.0.0/16, fe80::/10). Any other host (a name, a public IP,
an overlay-network address) caps the provider at ``personal`` and logs one
fixed-text warning (no variables), unless ``AETHER_OLLAMA_TRUST_HOST`` is set. The host value
is never logged.
"""

import asyncio
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

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

HostCategory = Literal["loopback", "private", "link_local", "hostname", "other_ip"]

_TRUSTED_NETWORKS: dict[HostCategory, tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = {
    "loopback": (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")),
    "private": (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ),
    "link_local": (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10")),
}
TRUSTED_CATEGORIES: frozenset[HostCategory] = frozenset(_TRUSTED_NETWORKS)


def classify_host(host_url: str) -> HostCategory:
    """Category of the host in an Ollama URL, from its text only (no DNS lookup).

    Only the exact name ``localhost`` counts as trusted (not ``localhost.``); every
    other name is ``hostname``. ``localhost`` is still resolved by the OS on connect.
    IPv4-mapped IPv6 and other forms outside the listed ranges are ``other_ip``.
    """
    host = httpx.URL(host_url).host.lower()
    if host == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return "hostname"
    for category, networks in _TRUSTED_NETWORKS.items():
        if any(address.version == n.version and address in n for n in networks):
            return category
    return "other_ip"


class OllamaProvider(Provider):
    def __init__(
        self,
        host: str,
        default_model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_host: bool = False,
    ) -> None:
        category = classify_host(host)
        trusted = category in TRUSTED_CATEGORIES or trust_host
        super().__init__(
            ProviderInfo(
                name=PROVIDER_NAME,
                kind="local",
                default_model=default_model,
                # Local inference may receive Sensitive context, but only on a trusted
                # host (#10); never Secret (docs/05 §7.1).
                max_privacy="sensitive" if trusted else "personal",
            )
        )
        self.host_category: HostCategory = category
        if not trusted:
            # Fixed text only: no variable reaches the log call, so the host can't
            # leak. The category stays on the provider (host_category).
            logger.warning(
                "OLLAMA_HOST is not loopback or a private address; Ollama is capped at "
                "personal and will refuse sensitive requests. Set AETHER_OLLAMA_TRUST_HOST "
                "only if you deliberately trust that host.",
            )
        elif category not in TRUSTED_CATEGORIES:
            logger.warning(
                "OLLAMA_HOST is not loopback or a private address but is trusted by "
                "AETHER_OLLAMA_TRUST_HOST; Ollama may receive sensitive requests.",
            )
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(None, connect=CONNECT_TIMEOUT_S),
            # Host trust (#10) is checked on the configured URL only, so the client must
            # not go anywhere else: no redirects (the SDK defaults to following them,
            # which would re-send a Sensitive body to the redirect target) and no
            # proxies from HTTP_PROXY/HTTPS_PROXY/ALL_PROXY.
            "follow_redirects": False,
            "trust_env": False,
        }
        if transport is not None:
            client_kwargs["transport"] = transport  # tests inject httpx.MockTransport
        self._client = AsyncClient(host=host, **client_kwargs)

    @classmethod
    def from_settings(cls, settings: Settings) -> "OllamaProvider":
        return cls(
            host=str(settings.ollama_host),
            default_model=settings.ollama_model,
            trust_host=settings.aether_ollama_trust_host,
        )

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
