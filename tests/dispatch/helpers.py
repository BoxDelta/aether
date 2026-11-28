"""Shared fakes for dispatcher tests: a temp env file and a fake GitHub API."""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from dispatch import github
from dispatch.config import DispatchSettings, load_settings

OWNER = "BoxDelta"
REPO = "aether"
LITELLM_URL = "http://127.0.0.1:4000"


def fake_value(kind: str) -> str:
    return f"fake-{kind}-" + secrets.token_hex(6)


def fake_webhook() -> str:
    # Joined at runtime so no webhook-shaped literal is committed (gitleaks).
    return (
        "https://" + "discord.com" + "/api/" + "webhooks/" + "123" + "/fake" + secrets.token_hex(8)
    )


def write_env(path: Path, **overrides: str) -> Path:
    values = {
        "DISPATCH_GITHUB_TOKEN": fake_value("gh"),
        "DISPATCH_LITELLM_KEY": fake_value("llm"),
        "DISPATCH_LITELLM_URL": LITELLM_URL,
        "DISPATCH_OWNER": OWNER,
        "DISPATCH_REPO": REPO,
        "DISPATCH_DISCORD_WEBHOOK": fake_webhook(),
        **overrides,
    }
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v != ""), "utf-8")
    path.chmod(0o600)
    return path


def make_settings(tmp_path: Path) -> DispatchSettings:
    return load_settings(write_env(tmp_path / "dispatch.env"))


def issue_json(
    number: int,
    author: str = OWNER,
    labels: tuple[str, ...] = (github.AUTO_LABEL, github.LOCAL_OK_LABEL),
    title: str = "Fix a typo in docs/CODE_TOUR.md",
    body: str = "One word is misspelled.",
    pull_request: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "number": number,
        "title": title,
        "body": body,
        "user": {"login": author},
        "labels": [{"name": name} for name in labels],
    }
    if pull_request:
        item["pull_request"] = {"url": "x"}
    return item


BOT = "boxdelta-dispatch"


def labeled(actor: str, label: str = github.AUTO_LABEL, kind: str = "User") -> dict[str, Any]:
    return {"event": "labeled", "actor": {"login": actor, "type": kind}, "label": {"name": label}}


def owner_labels() -> list[dict[str, Any]]:
    """The owner added both dispatch:auto and dispatch:local-ok."""
    return [labeled(OWNER), labeled(OWNER, github.LOCAL_OK_LABEL)]


@dataclass
class FakeGitHub:
    """Answers the read-only endpoints the dispatcher uses; records every request."""

    issues: list[dict[str, Any]] = field(default_factory=list)
    events: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    token_login: str = BOT
    token_scopes: str = "public_repo"  # noqa: S105 - an OAuth scope name, not a secret

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = f"/repos/{OWNER}/{REPO}/issues"
        path = request.url.path
        if path == "/user":
            headers = {
                "x-oauth-scopes": self.token_scopes,
                "github-authentication-token-expiration": "2027-02-25 12:00:00 UTC",
            }
            return httpx.Response(200, json={"login": self.token_login}, headers=headers)
        if path == base:
            return httpx.Response(200, json=self.issues)
        number = int(path.removeprefix(base + "/").split("/")[0])
        if path.endswith("/timeline"):
            return httpx.Response(200, json=self.events.get(number, []))
        for item in self.issues:
            if item["number"] == number:
                return httpx.Response(200, json=item)
        return httpx.Response(404, json={"message": "Not Found"})

    def client(self, settings: DispatchSettings) -> httpx.Client:
        return github.make_client(settings, transport=httpx.MockTransport(self.handler))


def chat_reply(content: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def route_json(route: str, reason: str = "small docs change") -> str:
    return json.dumps({"route": route, "reason": reason})
