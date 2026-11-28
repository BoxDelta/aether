"""scripts/identity_continuity.py (#23) with fake providers: no network."""

import asyncio
import re

import pytest

from core.config import Settings
from core.identity import load_identity
from core.log import REDACTED, Redactor, SecretCheck
from router.contract import (
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderInfo,
    ProviderTimeout,
    Usage,
)
from router.llama_swap import LlamaSwapProvider
from scripts.identity_continuity import (
    PROMPTS,
    load_prompts,
    make_provider,
    parse_args,
    render,
    run,
    summary,
)

FAKE_KEY = "sk-ant-api03-FAKE-continuity-0123456789"  # gitleaks:allow


class Echo(Provider):
    def __init__(self, fail_on: str | None = None) -> None:
        super().__init__(
            ProviderInfo(name="ollama", kind="local", default_model="m", max_privacy="sensitive")
        )
        self.requests: list[ModelRequest] = []
        self.fail_on = fail_on

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.fail_on and self.fail_on in request.messages[0].content:
            raise ProviderTimeout("ollama", "Ollama did not respond in time")
        return ModelResponse(
            text=f"I'm Aether.\nleaked {FAKE_KEY}", provider="ollama", model="m", kind="local"
        )

    async def check_available(self) -> bool:
        return True


def test_ten_generic_prompts_with_checks() -> None:
    prompts = load_prompts()
    assert len(prompts) == 10
    assert len({p["id"] for p in prompts}) == 10
    for p in prompts:
        assert set(p["checks"]) <= {"name", "tone", "rules"} and p["checks"]
    text = PROMPTS.read_text()
    # No personal data: no emails, phone numbers, URLs or key-shaped strings.
    assert not re.search(r"@\w|https?://|\+?\d[\d -]{7,}|sk-|BoxDelta", text)


def test_every_prompt_goes_through_identity_as_public() -> None:
    provider = Echo()
    results = asyncio.run(
        run(provider, load_identity(), load_prompts(), secret_check=SecretCheck())
    )

    assert len(provider.requests) == 10
    for request in provider.requests:
        assert request.privacy == "public"
        assert request.system is not None and "Aether" in request.system  # identity applied
    assert all(r["error"] is None and r["answer"].startswith("I'm Aether") for r in results)


def test_provider_error_recorded_not_raised() -> None:
    results = asyncio.run(
        run(Echo(fail_on="API"), load_identity(), load_prompts(), secret_check=SecretCheck())
    )
    [failed] = [r for r in results if r["error"]]
    assert failed["id"] == "explain"
    assert failed["error"] == "ProviderTimeout: Ollama did not respond in time"


@pytest.mark.parametrize("fail_on", [None, "API"])
def test_render_redacts_and_has_review_boxes(fail_on: str | None) -> None:
    results = asyncio.run(
        run(Echo(fail_on), load_identity(), load_prompts(), secret_check=SecretCheck())
    )
    text = render("ollama", load_identity(), results, Redactor([FAKE_KEY]))

    assert FAKE_KEY not in text and REDACTED in text
    assert text.count("Name: ☐ Tone: ☐ Rules: ☐") == 10
    assert "Identity: Aether v1" in text


# --- output token limit and totals (#77) --------------------------------------------


class Counting(Echo):
    """Echo with usage; the prompt containing ``cut_on`` is cut off."""

    def __init__(self, cut_on: str | None = None) -> None:
        super().__init__()
        self.cut_on = cut_on

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        cut = self.cut_on is not None and self.cut_on in request.messages[0].content
        return ModelResponse(
            text="" if cut else "I'm Aether.",
            provider="ollama",
            model="m",
            kind="local",
            finish_reason="length" if cut else "stop",
            usage=Usage(input_tokens=100, output_tokens=20),
        )


def test_no_limit_by_default() -> None:
    provider = Echo()
    asyncio.run(run(provider, load_identity(), load_prompts(), secret_check=SecretCheck()))

    assert all(r.max_tokens is None for r in provider.requests)


def test_max_tokens_reaches_every_request() -> None:
    provider = Echo()
    asyncio.run(
        run(provider, load_identity(), load_prompts(), max_tokens=256, secret_check=SecretCheck())
    )

    assert [r.max_tokens for r in provider.requests] == [256] * 10


def test_summary_has_token_totals_and_cut_off_count() -> None:
    results = asyncio.run(
        run(Counting(cut_on="API"), load_identity(), load_prompts(), secret_check=SecretCheck())
    )

    line = summary(results)

    assert line == "10 prompts, 0 errors, 1 cut off, tokens: 1000 input / 200 output"
    assert "Aether" not in line  # counts only, never answer text


def test_summary_without_usage_says_n_a() -> None:
    results = asyncio.run(
        run(Echo(fail_on="API"), load_identity(), load_prompts(), secret_check=SecretCheck())
    )

    assert summary(results) == "10 prompts, 1 errors, 0 cut off, tokens: n/a input / n/a output"


def test_render_shows_tokens_and_finish_reason() -> None:
    results = asyncio.run(
        run(Counting(cut_on="API"), load_identity(), load_prompts(), secret_check=SecretCheck())
    )
    text = render("ollama", load_identity(), results, Redactor([]))

    assert "finish: length, 100 / 20 tokens" in text
    assert text.count("100 / 20 tokens") == 10


def test_parse_args_default_and_valid_limit() -> None:
    assert parse_args(["claude"]).max_tokens is None
    assert parse_args(["claude", "--max-tokens", "1024"]).max_tokens == 1024
    assert parse_args(["ollama", "--max-tokens", "32000"]).max_tokens == 32000


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "12.5", "32001", "\uff11"])
def test_parse_args_rejects_bad_limits(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["claude", "--max-tokens", bad])

    assert "must be a whole number from 1 to 32000" in capsys.readouterr().err


def test_llama_swap_is_a_provider_choice() -> None:
    assert parse_args(["llama-swap"]).provider == "llama-swap"
    settings = Settings(_env_file=None, llama_swap_model="qwen-test")
    assert isinstance(make_provider("llama-swap", settings), LlamaSwapProvider)
