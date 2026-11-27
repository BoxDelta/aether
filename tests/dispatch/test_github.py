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
