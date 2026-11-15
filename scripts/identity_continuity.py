"""Identity continuity run (docs/07 §5, #23): 10 prompts through one real adapter.

Sends each prompt in ``tests/identity/prompts.yaml`` through ``call_with_identity``
(the only allowed way to call a provider) and writes the answers to a Markdown
file for the owner to review. No Core, no events, no database.

Run (fish), only with the owner's approval:
    uv run python -m scripts.identity_continuity ollama
    uv run python -m scripts.identity_continuity claude   # needs ANTHROPIC_API_KEY
    uv run python -m scripts.identity_continuity claude --max-tokens 1024

``--max-tokens`` limits each answer (thinking included for Claude). Claude calls
never exceed AETHER_CLOUD_MAX_OUTPUT_TOKENS (default 4096) either way (#77). The
summary line reports token totals and how many answers were cut off; counts only,
never answer text.

Reads configuration from the environment and ``.env`` (Ollama host and model,
Claude key) like Core does. The key is never written to the results.
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from core.config import CLOUD_MAX_OUTPUT_TOKENS_LIMIT, Settings, get_settings
from core.identity import Identity, load_identity
from core.log import Redactor, secrets_from
from router.claude import ClaudeProvider
from router.contract import Message, ModelRequest, Provider, ProviderError
from router.identity_call import call_with_identity
from router.ollama import OllamaProvider

REPO = Path(__file__).resolve().parent.parent
PROMPTS = REPO / "tests" / "identity" / "prompts.yaml"
RESULTS_DIR = REPO / "tests" / "identity"
TIMEOUT_S = 120.0


def load_prompts(path: Path = PROMPTS) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = yaml.safe_load(path.read_text())["prompts"]
    return prompts


def make_provider(name: str, settings: Settings) -> Provider:
    if name == "ollama":
        return OllamaProvider.from_settings(settings)
    return ClaudeProvider.from_settings(settings)


async def run(
    provider: Provider,
    identity: Identity,
    prompts: list[dict[str, Any]],
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    results = []
    for prompt in prompts:
        request = ModelRequest(
            messages=(Message(role="user", content=prompt["text"]),),
            privacy="public",
            timeout_s=TIMEOUT_S,
            max_tokens=max_tokens,
        )
        start = time.monotonic()
        try:
            response = await call_with_identity(provider, request, identity)
            answer, model, finish = response.text, response.model, response.finish_reason
            usage = response.usage
            error = None
        except ProviderError as exc:
            answer, model, finish, usage = "", None, None, None
            error = f"{type(exc).__name__}: {exc.message}"
        results.append(
            {
                **prompt,
                "answer": answer,
                "model": model,
                "finish_reason": finish,
                "error": error,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "seconds": round(time.monotonic() - start, 1),
            }
        )
    return results


def summary(results: list[dict[str, Any]]) -> str:
    """One line of counts for the terminal: never answer text."""
    failed = sum(1 for r in results if r["error"])
    cut_off = sum(1 for r in results if r["finish_reason"] == "length")

    def total(key: str) -> str:
        values = [r[key] for r in results if r[key] is not None]
        return str(sum(values)) if values else "n/a"

    return (
        f"{len(results)} prompts, {failed} errors, {cut_off} cut off, "
        f"tokens: {total('input_tokens')} input / {total('output_tokens')} output"
    )


def max_tokens_arg(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= CLOUD_MAX_OUTPUT_TOKENS_LIMIT:
        raise argparse.ArgumentTypeError(
            f"must be a whole number from 1 to {CLOUD_MAX_OUTPUT_TOKENS_LIMIT}"
        )
    return int(raw)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("provider", choices=["ollama", "claude"])
    parser.add_argument(
        "--max-tokens",
        type=max_tokens_arg,
        default=None,
        help="output token limit per answer (Claude: never above "
        "AETHER_CLOUD_MAX_OUTPUT_TOKENS, default 4096)",
    )
    return parser.parse_args(argv)


def render(
    provider_name: str, identity: Identity, results: list[dict[str, Any]], redact: Redactor
) -> str:
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    models = sorted({r["model"] for r in results if r["model"]})
    lines = [
        f"# Identity continuity: {provider_name}",
        "",
        f"- Run: {when}",
        f"- Identity: {identity.name} v{identity.version}",
        f"- Model: {', '.join(models) or 'n/a'}",
        "- Owner review: fill in each Name / Tone / Rules box (OK, or what's wrong).",
        "",
    ]
    for r in results:
        lines += [f"## {r['id']}: {r['text']}", ""]
        if r["error"]:
            lines += [f"**Error:** {redact(r['error'])}", ""]
        else:
            tokens = f"{r['input_tokens']} / {r['output_tokens']} tokens"
            meta = f"{r['model']}, {r['seconds']} s, finish: {r['finish_reason']}, {tokens}"
            lines += [f"*{meta}*", ""]
            lines += ["> " + line if line else ">" for line in redact(r["answer"]).splitlines()]
            lines.append("")
        lines += [f"Checks: {', '.join(r['checks'])}. Name: ☐ Tone: ☐ Rules: ☐", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    settings = get_settings()
    identity = load_identity()
    provider = make_provider(args.provider, settings)

    async def go() -> list[dict[str, Any]]:
        try:
            if not await provider.check_available():
                return []
            return await run(provider, identity, load_prompts(), args.max_tokens)
        finally:
            await provider.aclose()

    results = asyncio.run(go())
    if not results:
        print(f"{args.provider} is not available (not running, model not pulled, or no key)")
        return 1
    out = RESULTS_DIR / f"results-{args.provider}.md"
    out.write_text(render(args.provider, identity, results, Redactor(secrets_from(settings))))
    failed = sum(1 for r in results if r["error"])
    print(f"wrote {out.relative_to(REPO)}: {summary(results)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
