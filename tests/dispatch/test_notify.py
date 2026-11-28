"""Discord posts (ADR-030 §Claude path): only "#<n> brief ready", mentions off,
failures don't stop a run and are tried again next run."""

import json
from pathlib import Path

import httpx
import pytest

from dispatch import __main__ as cli
from dispatch import notify, state
from tests.dispatch.helpers import make_settings


class FakeDiscord:
    def __init__(self, statuses: list[int] | None = None, error: bool = False) -> None:
        self.statuses = statuses or []
        self.error = error
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(self.statuses.pop(0) if self.statuses else 204)

    def client(self) -> httpx.Client:
        return notify.make_client(httpx.MockTransport(self.handler))

    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


def test_message_is_number_and_brief_ready_only() -> None:
    assert notify.brief_ready(12) == "#12 brief ready"


def test_allowed_mentions_always_empty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeDiscord()
    with fake.client() as client:
        assert notify.deliver(client, settings, "#1 brief ready")
    payload = fake.payloads()[0]
    assert payload == {
        "content": "#1 brief ready",
        "allowed_mentions": {"parse": []},
        "flags": notify.SUPPRESS_EMBEDS,
    }
    assert fake.requests[0].method == "POST"
    assert fake.requests[0].url.host == "discord.com"


def test_post_never_prints_the_webhook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    assert settings.dispatch_discord_webhook is not None
    secret = settings.dispatch_discord_webhook.get_secret_value()
    for fake in (FakeDiscord([500]), FakeDiscord(error=True)):
        with fake.client() as client:
            assert not notify.deliver(client, settings, "x")
    err = capsys.readouterr().err
    assert "discord: post failed (HTTP 500)" in err
    assert "discord: post failed (ConnectError)" in err
    assert secret not in err
    assert "/api/webhooks/" not in err


def test_announce_records_only_accepted_and_retries_next_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path = tmp_path / "state.json"
    st = state.State(briefed={1, 2, 3}, announced={1})
    fake = FakeDiscord([500, 204])
    with fake.client() as client:
        assert cli.announce(st, settings, client, path) == 1
    assert [p["content"] for p in fake.payloads()] == ["#2 brief ready", "#3 brief ready"]
    assert state.load(path).announced == {1, 3}
    retry = FakeDiscord()
    with retry.client() as client:
        assert cli.announce(state.load(path), settings, client, path) == 1
    assert [p["content"] for p in retry.payloads()] == ["#2 brief ready"]
    assert state.load(path).announced == {1, 2, 3}


def test_client_with_credentials_is_refused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeDiscord()
    client = httpx.Client(
        headers={"Authorization": "Bearer fake"}, transport=httpx.MockTransport(fake.handler)
    )
    with client:
        assert not notify.deliver(client, settings, "x")
    assert fake.requests == []


def test_webhook_request_has_no_authorization(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeDiscord()
    with fake.client() as client:
        notify.deliver(client, settings, "x")
    assert "authorization" not in fake.requests[0].headers
