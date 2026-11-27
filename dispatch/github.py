"""Read-only GitHub access for the dispatcher (ADR-030 §Trust).

Step 4a only reads. The client refuses any method but GET before a request
leaves the process, and the token is only ever sent to ``api.github.com``.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from dispatch.config import DispatchSettings

API_HOST = "api.github.com"
API_URL = f"https://{API_HOST}"
AUTO_LABEL = "dispatch:auto"
READ_METHODS = frozenset({"GET"})
TIMEOUT_S = 20.0
MAX_PAGES = 20


class GitHubError(Exception):
    """A GitHub call failed or answered something unexpected."""


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str
    author: str
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustCheck:
    ok: bool
    reason: str


def _refuse_writes(request: httpx.Request) -> None:
    if request.method not in READ_METHODS:
        raise GitHubError(f"refused {request.method}: the dispatcher is read-only in 4a")
    if request.url.host != API_HOST or request.url.scheme != "https":
        raise GitHubError("refused a request outside api.github.com")


def make_client(
    settings: DispatchSettings, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    return httpx.Client(
        base_url=API_URL,
        headers={
            "Authorization": f"Bearer {settings.dispatch_github_token.get_secret_value()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=TIMEOUT_S,
        follow_redirects=False,
        event_hooks={"request": [_refuse_writes]},
        transport=transport,
    )


def _get(client: httpx.Client, url: str, params: dict[str, str] | None = None) -> httpx.Response:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from None


def _pages(client: httpx.Client, path: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
    url: str | None = path
    query: dict[str, str] | None = {**params, "per_page": "100"}
    for _ in range(MAX_PAGES):
        if url is None:
            return
        response = _get(client, url, query)
        items = response.json()
        if not isinstance(items, list):
            raise GitHubError("GitHub answered a non-list page")
        yield from (item for item in items if isinstance(item, dict))
        url = response.links.get("next", {}).get("url")
        query = None  # the next link already carries the query
    raise GitHubError(f"more than {MAX_PAGES} pages")


def _to_issue(item: dict[str, Any]) -> Issue | None:
    """None for pull requests (the issues API lists both)."""
    if "pull_request" in item:
        return None
    labels = tuple(
        str(label["name"])
        for label in item.get("labels") or []
        if isinstance(label, dict) and "name" in label
    )
    return Issue(
        number=int(item["number"]),
        title=str(item.get("title") or ""),
        body=str(item.get("body") or ""),
        author=str((item.get("user") or {}).get("login") or ""),
        labels=labels,
    )


def _repo_path(settings: DispatchSettings) -> str:
    return f"/repos/{settings.dispatch_owner}/{settings.dispatch_repo}"


def list_auto_issues(client: httpx.Client, settings: DispatchSettings) -> list[Issue]:
    """Open issues labelled ``dispatch:auto``, pull requests skipped. Not yet trusted."""
    params = {"state": "open", "labels": AUTO_LABEL}
    items = _pages(client, f"{_repo_path(settings)}/issues", params)
    return [issue for item in items if (issue := _to_issue(item)) is not None]


def get_issue(client: httpx.Client, settings: DispatchSettings, number: int) -> Issue | None:
    """One issue, or None if it is a pull request."""
    data = _get(client, f"{_repo_path(settings)}/issues/{number}").json()
    if not isinstance(data, dict):
        raise GitHubError("GitHub answered a non-object issue")
    return _to_issue(data)


def check_author(issue: Issue, settings: DispatchSettings) -> TrustCheck:
    if issue.author != settings.dispatch_owner:
        return TrustCheck(False, "not opened by the owner")
    return TrustCheck(True, "opened by the owner")


def label_adder(client: httpx.Client, settings: DispatchSettings, number: int) -> str | None:
    """Who added ``dispatch:auto`` last, from the issue's event history."""
    adder: str | None = None
    for event in _pages(client, f"{_repo_path(settings)}/issues/{number}/events", {}):
        label = event.get("label") or {}
        if event.get("event") == "labeled" and label.get("name") == AUTO_LABEL:
            adder = str((event.get("actor") or {}).get("login") or "")
    return adder


def check_trust(client: httpx.Client, settings: DispatchSettings, issue: Issue) -> TrustCheck:
    """Both ADR-030 checks: opened by the owner and ``dispatch:auto`` added by the owner."""
    author = check_author(issue, settings)
    if not author.ok:
        return author
    if AUTO_LABEL not in issue.labels:
        return TrustCheck(False, f"no {AUTO_LABEL} label")
    adder = label_adder(client, settings, issue.number)
    if adder is None:
        return TrustCheck(False, f"no {AUTO_LABEL} label event")
    if adder != settings.dispatch_owner:
        return TrustCheck(False, f"{AUTO_LABEL} not added by the owner")
    return TrustCheck(True, "opened and labelled by the owner")
