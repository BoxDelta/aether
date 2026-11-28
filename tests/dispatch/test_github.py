"""Owner checks and read-only GitHub access (ADR-030 §Trust)."""

from pathlib import Path

import httpx
import pytest

from dispatch import github
from tests.dispatch.helpers import OWNER, FakeGitHub, issue_json, labeled, make_settings


def _issue(**kwargs: object) -> github.Issue:
    item = issue_json(7, **kwargs)  # type: ignore[arg-type]
    issue = github._to_issue(item)
    assert issue is not None
    return issue


def test_trusted_when_owner_opened_and_labelled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(issues=[issue_json(7)], events={7: [labeled(OWNER)]})
    with fake.client(settings) as client:
        assert github.check_trust(client, settings, _issue()).ok


def test_wrong_author(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(events={7: [labeled(OWNER)]})
    with fake.client(settings) as client:
        check = github.check_trust(client, settings, _issue(author="mallory"))
    assert not check.ok
    assert check.reason == "not opened by the owner"
    assert fake.requests == []  # rejected before any event lookup


def test_label_added_by_someone_else(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(events={7: [labeled("mallory")]})
    with fake.client(settings) as client:
        check = github.check_trust(client, settings, _issue())
    assert not check.ok
    assert "not added by the owner" in check.reason


def test_latest_labeled_event_decides(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    removed = {"event": "unlabeled", "actor": {"login": OWNER}, "label": {"name": "dispatch:auto"}}
    events = [labeled(OWNER), removed, labeled("mallory"), labeled(OWNER, "docs")]
    fake = FakeGitHub(events={7: events})
    with fake.client(settings) as client:
        assert not github.check_trust(client, settings, _issue()).ok


def test_label_missing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(events={7: [labeled(OWNER)]})
    with fake.client(settings) as client:
        check = github.check_trust(client, settings, _issue(labels=("docs",)))
    assert not check.ok
    assert "no dispatch:auto label" in check.reason


def test_label_present_but_no_event(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(events={7: []})
    with fake.client(settings) as client:
        assert not github.check_trust(client, settings, _issue()).ok


def test_list_skips_pull_requests(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(issues=[issue_json(1), issue_json(2, pull_request=True)])
    with fake.client(settings) as client:
        issues = github.list_auto_issues(client, settings)
    assert [i.number for i in issues] == [1]
    query = fake.requests[0].url.params
    assert query["labels"] == "dispatch:auto"
    assert query["state"] == "open"


def test_get_issue_returns_none_for_pull_request(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(issues=[issue_json(3, pull_request=True)])
    with fake.client(settings) as client:
        assert github.get_issue(client, settings, 3) is None


def test_pagination_follows_next_link_on_github_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[labeled("mallory")])
        nxt = f'<{github.API_URL}/repos/{OWNER}/aether/issues/7/events?page=2>; rel="next"'
        return httpx.Response(200, json=[labeled(OWNER)], headers={"Link": nxt})

    with github.make_client(settings, httpx.MockTransport(handler)) as client:
        assert github.label_adder(client, settings, 7) == "mallory"
    assert len(seen) == 2


def test_next_link_to_another_host_is_refused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        nxt = '<https://evil.example/steal?page=2>; rel="next"'
        return httpx.Response(200, json=[], headers={"Link": nxt})

    with (
        github.make_client(settings, httpx.MockTransport(handler)) as client,
        pytest.raises(github.GitHubError, match=r"outside api\.github\.com"),
    ):
        github.label_adder(client, settings, 7)


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_client_refuses_write_methods(tmp_path: Path, method: str) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub()
    with fake.client(settings) as client, pytest.raises(github.GitHubError, match="read-only"):
        client.request(method, "/repos/BoxDelta/aether/issues/1")
    assert fake.requests == []


def test_http_error_message_has_no_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    token = settings.dispatch_github_token.get_secret_value()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(401, json={"message": "Bad credentials"})

    with (
        github.make_client(settings, httpx.MockTransport(handler)) as client,
        pytest.raises(github.GitHubError) as info,
    ):
        github.list_auto_issues(client, settings)
    assert token not in str(info.value)


def _write_client(tmp_path: Path, seen: list[httpx.Request]) -> tuple[httpx.Client, object]:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"number": 90})

    return github.make_write_client(settings, httpx.MockTransport(handler)), settings


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", f"/repos/{OWNER}/aether/pulls/5/merge"),
        ("PATCH", f"/repos/{OWNER}/aether/issues/5"),
        ("DELETE", f"/repos/{OWNER}/aether/git/refs/heads/main"),
        ("POST", f"/repos/{OWNER}/aether/issues/5/comments"),
        ("POST", f"/repos/{OWNER}/aether/pulls/5/reviews"),
        ("POST", f"/repos/{OWNER}/aether/git/refs"),
    ],
)
def test_write_client_refuses_everything_but_two_calls(
    tmp_path: Path, method: str, path: str
) -> None:
    seen: list[httpx.Request] = []
    client, _ = _write_client(tmp_path, seen)
    with client, pytest.raises(github.GitHubError, match="not an allowed"):
        client.request(method, path)
    assert seen == []  # refused before it was sent


def test_write_client_opens_pr_and_adds_dispatcher_labels(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    client, settings = _write_client(tmp_path, seen)
    with client:
        number = github.open_pull_request(client, settings, "dispatch/5-x", "t", "b")  # type: ignore[arg-type]
        github.add_label(client, settings, number, github.LOCAL_PR_LABEL)  # type: ignore[arg-type]
    assert number == 90
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", f"/repos/{OWNER}/aether/pulls"),
        ("POST", f"/repos/{OWNER}/aether/issues/90/labels"),
    ]
    import json

    assert json.loads(seen[0].content)["base"] == "main"
    assert json.loads(seen[0].content)["head"] == "dispatch/5-x"


@pytest.mark.parametrize(
    "label", [github.AUTO_LABEL, github.LOCAL_OK_LABEL, "dispatch:claude-only", "security"]
)
def test_dispatcher_never_adds_owner_labels(tmp_path: Path, label: str) -> None:
    seen: list[httpx.Request] = []
    client, settings = _write_client(tmp_path, seen)
    with client, pytest.raises(github.GitHubError, match="never adds"):
        github.add_label(client, settings, 5, label)  # type: ignore[arg-type]
    assert seen == []


def test_pull_request_only_from_dispatch_branches(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    client, settings = _write_client(tmp_path, seen)
    with client, pytest.raises(github.GitHubError, match="dispatch/"):
        github.open_pull_request(client, settings, "main", "t", "b")  # type: ignore[arg-type]
    assert seen == []


def test_local_ok_needs_the_owner(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    issue = _issue()
    for events, ok in [
        ([labeled(OWNER, github.LOCAL_OK_LABEL)], True),
        ([labeled("mallory", github.LOCAL_OK_LABEL)], False),
        ([], False),
    ]:
        fake = FakeGitHub(events={7: events})
        with fake.client(settings) as client:
            assert github.check_local_ok(client, settings, issue).ok is ok
    no_label = _issue(labels=(github.AUTO_LABEL,))
    with FakeGitHub().client(settings) as client:
        assert (
            github.check_local_ok(client, settings, no_label).reason == "no dispatch:local-ok label"
        )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (f"/repos/{OWNER}/aether/issues/5/labels", {"labels": ["dispatch:local-ok"]}),
        (f"/repos/{OWNER}/aether/issues/5/labels", {"labels": ["dispatch:local", "security"]}),
        ("/repos/someone/else/pulls", {"title": "t"}),
        ("/repos/someone/else/issues/5/labels", {"labels": ["dispatch:local"]}),
    ],
)
def test_write_guard_checks_label_bodies_and_the_repo(
    tmp_path: Path, path: str, body: dict[str, object]
) -> None:
    seen: list[httpx.Request] = []
    client, _ = _write_client(tmp_path, seen)
    with client, pytest.raises(github.GitHubError, match="not an allowed"):
        client.post(path, json=body)
    assert seen == []
