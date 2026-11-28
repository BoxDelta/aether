"""Discord posts for the Claude path (ADR-030 §Claude path).

One message per brief, ``#<n> brief ready``, or per local pull request,
``#<n> local PR #<p> opened``. Nothing else: no issue title, body, reason, code or
file contents. Mentions are always off and link embeds suppressed.
A failed post prints a short error and the run carries on; the post is tried again
on the next run, never in a loop.
"""

import sys

import httpx

from dispatch.config import DispatchSettings

TIMEOUT_S = 15.0
SUPPRESS_EMBEDS = 1 << 2  # Discord message flag


def brief_ready(number: int) -> str:
    """The only message the Claude path posts. ``number`` is an int, never issue text."""
    return f"#{int(number)} brief ready"


def pr_opened(number: int, pr: int) -> str:
    """The local path's message: issue and pull request numbers only."""
    return f"#{int(number)} local PR #{int(pr)} opened"


def build_payload(content: str) -> dict[str, object]:
    return {"content": content, "allowed_mentions": {"parse": []}, "flags": SUPPRESS_EMBEDS}


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT_S, trust_env=False, follow_redirects=False, transport=transport
    )


def deliver(client: httpx.Client, settings: DispatchSettings, content: str) -> bool:
    """One try. Never raises; never prints the webhook URL."""
    webhook = settings.dispatch_discord_webhook
    if "authorization" in client.headers:
        # Never send another service's credential (the GitHub or LiteLLM client).
        print("discord: refused a client that carries credentials", file=sys.stderr)
        return False
    if webhook is None:
        print("discord: no webhook configured", file=sys.stderr)
        return False
    try:
        response = client.post(webhook.get_secret_value(), json=build_payload(content))
    except httpx.HTTPError as exc:
        print(f"discord: post failed ({type(exc).__name__})", file=sys.stderr)
        return False
    if not response.is_success:
        print(f"discord: post failed (HTTP {response.status_code})", file=sys.stderr)
        return False
    return True
