"""Post dry-run results to the owner's Discord channel (ADR-030, #135).

One message per triaged issue: number, link, route, step and the cleaned reason.
The issue title and body are never put in the message; a model-written reason may
quote up to 200 characters of them, neutralised. Mentions are always off and link embeds suppressed.
A failed post prints a short error and the run carries on; nothing is retried here
(an unposted issue is tried again on the next run).
"""

import fcntl
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from dispatch.classify import clean_reason
from dispatch.config import DispatchSettings

TIMEOUT_S = 15.0
SUPPRESS_EMBEDS = 1 << 2  # Discord message flag
STATE_FILE = Path(__file__).resolve().parents[1] / ".dispatch" / "state.json"
LOCK_FILE = STATE_FILE.with_name("notify.lock")

# Discord markdown: escape each with a backslash so a reason can't format or link.
_MARKDOWN = re.compile(r"([\\*_~`|>#\[\]()\-:<])")


class StateError(Exception):
    """The posted-issues state file can't be read."""


class Busy(Exception):
    """Another ``--notify`` run holds the lock."""


@contextmanager
def run_lock(path: Path = LOCK_FILE) -> Iterator[None]:
    """One ``--notify`` run at a time (timer and manual), so state isn't lost or
    issues posted twice. Non-blocking: a second run stops at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def neutralise(reason: str) -> str:
    """One clean line, no '@', Discord markdown escaped."""
    return _MARKDOWN.sub(r"\\\1", clean_reason(reason).replace("@", ""))


def issue_url(settings: DispatchSettings, number: int) -> str:
    return f"https://github.com/{settings.dispatch_owner}/{settings.dispatch_repo}/issues/{number}"


def format_message(
    settings: DispatchSettings, number: int, route: str, step: str, reason: str
) -> str:
    return f"#{number} → {route} ({step}): {neutralise(reason)}\n<{issue_url(settings, number)}>"


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


def load_posted(path: Path = STATE_FILE) -> set[int]:
    """Issue numbers already posted. A missing file is an empty set."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        raise StateError(f"state file unreadable: {path}") from exc
    posted = data.get("posted") if isinstance(data, dict) else None
    if not isinstance(posted, list) or not all(
        isinstance(n, int) and not isinstance(n, bool) for n in posted
    ):
        raise StateError(f"state file malformed: {path}")
    return set(posted)


def save_posted(posted: set[int], path: Path = STATE_FILE) -> None:
    """Atomic: write a temp file in the same folder, fsync, then rename over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"posted": sorted(posted)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
