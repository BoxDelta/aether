"""Discord posts (#135): mentions off, reason neutralised, failures don't stop a run."""

import json
import re
from pathlib import Path

import httpx
import pytest

from dispatch import __main__ as cli
from dispatch import classify, notify
from dispatch.github import Issue
from tests.dispatch.helpers import OWNER, FakeGitHub, issue_json, labeled, make_settings


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


def _row(n: int, reason: str = "docs only") -> cli.Row:
    return cli.Row(n, "local", "model", reason)


def test_message_format(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    text = notify.format_message(settings, 12, "claude", "rule", "mentions router/")
    assert text == (
        f"#12 → claude (rule): mentions router/\n<https://github.com/{OWNER}/aether/issues/12>"
    )


@pytest.mark.parametrize(
    "reason",
    [
        "@everyone look",
        "@here",
        "<@123456> and <@&999> and <#42>",
        "**bold** __u__ ~~s~~ `code` ||spoiler|| > quote # head [x](https://evil.example)",
        "line\nbreak\x1b[31m",
    ],
)
def test_reason_is_neutralised(reason: str) -> None:
    out = notify.neutralise(reason)
    assert "@" not in out
    assert "\n" not in out and "\x1b" not in out
    assert re.search(r"(?<!\\)[*_~`>#\[\]()|]", out) is None  # every markdown char escaped
    assert "https://" not in out  # no autolink


def test_allowed_mentions_always_empty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeDiscord()
    with fake.client() as client:
        assert notify.deliver(client, settings, "#1 → local (model): @everyone")
    payload = fake.payloads()[0]
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["flags"] == notify.SUPPRESS_EMBEDS
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


def test_post_new_records_only_accepted_and_carries_on(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    state = tmp_path / "state.json"
    fake = FakeDiscord([500, 204])
    posted: set[int] = {1}
    rows = [_row(2), _row(3), cli.Row(4, "-", "trust", "not opened by the owner")]
    with fake.client() as client:
        sent = cli.post_new(rows, settings, client, posted, state)
    assert sent == 1
    assert len(fake.requests) == 2  # one try each, trust rows never posted
    assert notify.load_posted(state) == {1, 3}


def test_no_body_or_title_in_post(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeDiscord()
    with fake.client() as client:
        cli.post_new([_row(5)], settings, client, set(), tmp_path / "s.json")
    content = str(fake.payloads()[0]["content"])
    assert content.startswith("#5 → local (model): docs only")
    assert "misspelled" not in content


def test_state_round_trip_and_atomic(tmp_path: Path) -> None:
    state = tmp_path / "sub" / "state.json"
    assert notify.load_posted(state) == set()
    notify.save_posted({3, 1}, state)
    assert json.loads(state.read_text()) == {"posted": [1, 3]}
    assert [p.name for p in state.parent.iterdir()] == ["state.json"]  # no temp left


@pytest.mark.parametrize("text", ["not json", '{"posted": "x"}', '{"posted": [true]}', "[]"])
def test_bad_state_is_an_error(tmp_path: Path, text: str) -> None:
    state = tmp_path / "state.json"
    state.write_text(text)
    with pytest.raises(notify.StateError):
        notify.load_posted(state)


def test_posted_issues_skip_trust_rules_and_model(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(
        issues=[issue_json(1), issue_json(2)], events={1: [labeled(OWNER)], 2: [labeled(OWNER)]}
    )
    seen: list[int] = []

    def triage(issue: Issue) -> classify.Decision:
        seen.append(issue.number)
        return classify.Decision("local", "model", "ok")

    with fake.client(settings) as gh:
        rows = cli.triage_rows(gh, settings, triage, None, frozenset({1}))
    assert seen == [2]
    assert [r.issue for r in rows] == [2]
    assert not any(r.url.path.endswith("/1/events") for r in fake.requests)


def test_state_file_is_at_repo_root() -> None:
    root = Path(__file__).resolve().parents[2]
    assert notify.STATE_FILE.relative_to(root) == Path(".dispatch/state.json")


def test_notify_without_webhook_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.dispatch.helpers import write_env

    path = write_env(tmp_path / "d.env", DISPATCH_DISCORD_WEBHOOK="")
    monkeypatch.setenv("DISPATCH_ENV_FILE", str(path))
    assert cli.main(["triage", "--notify"]) == 2
    assert "DISPATCH_DISCORD_WEBHOOK" in capsys.readouterr().err


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


def test_second_notify_run_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "notify.lock"
    with notify.run_lock(lock), pytest.raises(notify.Busy), notify.run_lock(lock):
        pass
    with notify.run_lock(lock):  # released after the first run
        pass
