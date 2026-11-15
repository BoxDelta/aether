"""Claude API adapter (cloud inference) implementing the Nexus contract.

Uses the official ``anthropic`` SDK. Design points:

- The API key comes only from ``Settings`` (``ANTHROPIC_API_KEY``). Without it no
  client is built, so the SDK never falls back to other credential sources
  (``ant auth`` profiles, other env vars) behind AETHER's back.
- The endpoint is fixed (#9): ``base_url`` is always ``ANTHROPIC_API_BASE_URL`` and
  ``auth_token`` is always ``None``. Checked against anthropic 2.8.0: without an
  explicit ``base_url`` the SDK reads ``ANTHROPIC_BASE_URL`` from the environment,
  so a stray variable would send the key and prompts to another host. With an
  explicit ``api_key`` it does not read ``ANTHROPIC_API_KEY``,
  ``ANTHROPIC_AUTH_TOKEN`` or profiles. It still merges ``ANTHROPIC_CUSTOM_HEADERS``
  into request headers, which can override ``Authorization`` but not the host
  (docs/05 §5, #18).
- Server-side refusal fallback is on (``fallbacks="default"``): if the model
  declines, the API re-runs the request on Anthropic's recommended fallback model
  inside the same call. If the whole chain declines, the response has
  ``finish_reason="refusal"`` and no text.
- Output is capped (#77): ``max_tokens`` is the request's own limit or
  ``AETHER_CLOUD_MAX_OUTPUT_TOKENS`` (default 4096), whichever is lower. Thinking
  tokens count toward it. A cut-off answer keeps its partial (possibly empty) text,
  has ``finish_reason="length"`` and logs one metadata-only warning.
- ``temperature`` is not sent: the API version this SDK uses (2026-10-21) rejects
  sampling parameters on every model.
- Error messages are built only from safe parts (status code, request id). SDK
  exception text is never copied, so it cannot carry the key or request content
  into contract errors, events or API responses.
"""

import logging

import anthropic
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import (
    BetaAssistantMessageParam,
    BetaMessage,
    BetaUserMessageParam,
)
from pydantic import SecretStr

from core.config import DEFAULT_CLOUD_MAX_OUTPUT_TOKENS, Settings
from router.contract import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderBadResponse,
    ProviderError,
    ProviderInfo,
    ProviderTimeout,
    ProviderUnavailable,
    Usage,
)

logger = logging.getLogger("aether.router.claude")

PROVIDER_NAME = "claude"

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# The only endpoint AETHER talks to (#9). Not configurable, and never taken from the
# environment.
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"


_FINISH_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "refusal",
}


class ClaudeProvider(Provider):
    def __init__(
        self,
        api_key: SecretStr | None,
        default_model: str,
        *,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
        max_retries: int = 2,
        max_output_tokens: int = DEFAULT_CLOUD_MAX_OUTPUT_TOKENS,
    ) -> None:
        super().__init__(
            ProviderInfo(
                name=PROVIDER_NAME,
                kind="cloud",
                default_model=default_model,
                # docs/05 §7.1: cloud gets Public and Personal; Sensitive needs per-request
                # owner approval (not modelled yet); Secret never.
                max_privacy="personal",
            )
        )
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")
        # Output token cap for every call (#77); thinking tokens count toward it.
        self._max_output_tokens = max_output_tokens
        self._client: AsyncAnthropic | None = None
        if api_key is not None:
            self._client = AsyncAnthropic(
                api_key=api_key.get_secret_value(),
                # The explicit api_key is what stops the SDK reading ANTHROPIC_API_KEY,
                # ANTHROPIC_AUTH_TOKEN or profiles; auth_token=None states that intent.
                # The explicit base_url stops it reading ANTHROPIC_BASE_URL (#9).
                auth_token=None,
                base_url=ANTHROPIC_API_BASE_URL,
                max_retries=max_retries,
                http_client=http_client,
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClaudeProvider":
        return cls(
            settings.anthropic_api_key,
            settings.anthropic_model,
            max_output_tokens=settings.cloud_max_output_tokens,
        )

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

    def output_limit(self, request: ModelRequest) -> int:
        """The max_tokens sent: the request's own limit, never above the cap.

        Core already rejects a /chat limit above the cap (422); this clamp is the
        backstop for every other caller.
        """
        if request.max_tokens is None:
            return self._max_output_tokens
        return min(request.max_tokens, self._max_output_tokens)

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        if self._client is None:
            raise ProviderUnavailable(self.name, "no API key configured", retryable=False)

        model = request.model or self.info.default_model
        limit = self.output_limit(request)
        messages: list[BetaUserMessageParam | BetaAssistantMessageParam] = [
            BetaUserMessageParam(role="user", content=m.content)
            if m.role == "user"
            else BetaAssistantMessageParam(role="assistant", content=m.content)
            for m in request.messages
        ]
        if request.temperature is not None:
            logger.debug("temperature not sent to Claude (unsupported by the API version)")

        try:
            raw = await self._client.with_options(timeout=request.timeout_s).beta.messages.create(
                model=model,
                max_tokens=limit,
                messages=messages,
                system=request.system if request.system else omit,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AnthropicError as exc:
            raise _map_error(exc) from exc

        response = self._to_response(raw)
        if response.finish_reason == "length":
            # Cut off: by the output cap, or by the context window. Metadata only.
            logger.warning(
                "Output cut off",
                extra={
                    "model": response.model,
                    "stop_reason": raw.stop_reason,
                    "max_tokens": limit,
                    "output_tokens": raw.usage.output_tokens,
                    "empty_text": response.text == "",
                },
            )
        return response

    def _to_response(self, raw: BetaMessage) -> ModelResponse:
        finish = _FINISH_REASONS.get(raw.stop_reason or "", "other")
        if finish == "refusal":
            category = raw.stop_details.category if raw.stop_details else None
            logger.info("Claude declined the request", extra={"refusal_category": category})
            text = ""  # discard any partial output of a declined turn
        else:
            text = "".join(block.text for block in raw.content if block.type == "text")
        return ModelResponse(
            text=text,
            provider=self.name,
            model=raw.model,
            kind="cloud",
            finish_reason=finish,
            usage=Usage(input_tokens=raw.usage.input_tokens, output_tokens=raw.usage.output_tokens),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def check_available(self) -> bool:
        """True if an API key is configured. No network call; failures surface per request."""
        return self._client is not None


def _map_error(exc: anthropic.AnthropicError) -> ProviderError:
    """Map SDK exceptions to contract errors using only safe parts of the exception."""
    # Order matters: APITimeoutError subclasses APIConnectionError.
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeout(PROVIDER_NAME, "Claude API did not respond in time")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailable(PROVIDER_NAME, "cannot connect to the Claude API")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        ref = (
            f" (HTTP {status}, request_id={exc.request_id})"
            if exc.request_id
            else f" (HTTP {status})"
        )
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return ProviderUnavailable(
                PROVIDER_NAME, f"authentication failed{ref}", retryable=False
            )
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderUnavailable(PROVIDER_NAME, f"model not found{ref}", retryable=False)
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderUnavailable(PROVIDER_NAME, f"rate limited{ref}")
        if isinstance(exc, anthropic.RequestTimeoutError):
            return ProviderTimeout(PROVIDER_NAME, f"request timed out{ref}")
        if status >= 500:
            return ProviderUnavailable(PROVIDER_NAME, f"server error or overloaded{ref}")
        return ProviderBadResponse(PROVIDER_NAME, f"request rejected{ref}")
    return ProviderBadResponse(PROVIDER_NAME, f"unexpected {type(exc).__name__} from the SDK")
