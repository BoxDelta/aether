"""GitHub access for the dispatcher (ADR-030 §Trust, §Local path).

The read client refuses any method but GET before a request leaves the process.
The write client (step 4b) allows exactly two calls: open a pull request and add
labels to an issue or pull request. Nothing can merge, close, edit, delete or
comment. The token is only ever sent to ``api.github.com``.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from dispatch.config import DispatchSettings

API_HOST = "api.github.com"
API_URL = f"https://{API_HOST}"
AUTO_LABEL = "dispatch:auto"
LOCAL_OK_LABEL = "dispatch:local-ok"
LOCAL_PR_LABEL = "dispatch:local"
CLAUDE_LABEL = "dispatch:claude"
READ_METHODS = frozenset({"GET"})
# Step 4b writes. Anything else is refused before it is sent, label bodies too.
# The token belongs to the machine user (ADR-041), so its labels show as the bot
# and the trust checks below never count them.
ALLOWED_LABEL_BODIES = (b'{"labels":["dispatch:local"]}', b'{"labels":["dispatch:claude"]}')
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


def _write_guard(settings: DispatchSettings) -> Callable[[httpx.Request], None]:
    repo = re.escape(_repo_path(settings))
    pulls = re.compile(repo + r"/pulls")
    labels = re.compile(repo + r"/issues/\d+/labels")

    def guard(request: httpx.Request) -> None:
        if request.url.host != API_HOST or request.url.scheme != "https":
            raise GitHubError("refused a request outside api.github.com")
        if request.method in READ_METHODS:
            return
        path, body = request.url.path, request.content.replace(b" ", b"")
        allowed = request.method == "POST" and (
            pulls.fullmatch(path) is not None
            or (labels.fullmatch(path) is not None and body in ALLOWED_LABEL_BODIES)
        )
        if not allowed:
            raise GitHubError(f"refused {request.method}: not an allowed dispatcher write")

    return guard


def make_write_client(
    settings: DispatchSettings, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """For step 4b only: GET, open a pull request, add a dispatcher label. Nothing else."""
    return _client(settings, _write_guard(settings), transport)


def make_client(
    settings: DispatchSettings, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    return _client(settings, _refuse_writes, transport)


def _client(
    settings: DispatchSettings,
    hook: Callable[[httpx.Request], None],
    transport: httpx.BaseTransport | None,
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
        trust_env=False,  # no proxy, CA bundle or netrc from the shell (docs/05 §5)
        event_hooks={"request": [hook]},
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


@dataclass(frozen=True, slots=True)
class Actor:
    login: str
    kind: str  # GitHub's account type: "User", "Bot", "Organization"
    via_app: bool = False  # an app acting for the user (performed_via_github_app)


def _actor(event: dict[str, Any]) -> Actor:
    actor = event.get("actor") or {}
    return Actor(
        str(actor.get("login") or ""),
        str(actor.get("type") or ""),
        event.get("performed_via_github_app") is not None,
    )


def _timeline(
    client: httpx.Client, settings: DispatchSettings, number: int
) -> list[dict[str, Any]]:
    return list(_pages(client, f"{_repo_path(settings)}/issues/{number}/timeline", {}))


def label_adder(
    client: httpx.Client, settings: DispatchSettings, number: int, name: str = AUTO_LABEL
) -> Actor | None:
    """Who added label ``name`` last, from the issue's timeline events (ADR-041)."""
    adder: Actor | None = None
    for event in _timeline(client, settings, number):
        label = event.get("label") or {}
        if event.get("event") == "labeled" and label.get("name") == name:
            adder = _actor(event)
    return adder


def renamed_by_other(client: httpx.Client, settings: DispatchSettings, number: int) -> bool:
    """Someone other than the owner changed the issue's title (ADR-041). Body edits
    are not in the timeline (#216)."""
    return any(
        event.get("event") == "renamed" and not is_owner(_actor(event), settings)
        for event in _timeline(client, settings, number)
    )


def is_owner(actor: Actor | None, settings: DispatchSettings) -> bool:
    """The owner's own account: the login matches and it is a person, never the
    dispatcher's machine user or an app."""
    return (
        actor is not None
        and actor.login == settings.dispatch_owner
        and actor.kind == "User"
        and not actor.via_app
        and actor.login != settings.dispatch_bot_login
    )


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    ok: bool
    reason: str
    expires: str | None = None


def check_token_identity(client: httpx.Client, settings: DispatchSettings) -> TokenIdentity:
    """The token is the machine user's (ADR-041): its login is ``dispatch_bot_login``,
    never the owner's, and its only scope is ``public_repo``. The local path writes
    only after this passes."""
    response = _get(client, "/user")
    data = response.json()
    login = str(data.get("login") or "") if isinstance(data, dict) else ""
    if login == settings.dispatch_owner:
        return TokenIdentity(False, "the token is the owner's, not the machine user's")
    if login != settings.dispatch_bot_login:
        return TokenIdentity(False, "the token is not the machine user's")
    scopes = {s.strip() for s in response.headers.get("x-oauth-scopes", "").split(",")}
    if scopes != {"public_repo"}:
        return TokenIdentity(False, "the token's scopes are not exactly public_repo")
    expires = response.headers.get("github-authentication-token-expiration")
    return TokenIdentity(True, "machine user token", expires)


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
    if not is_owner(adder, settings):
        return TrustCheck(False, f"{AUTO_LABEL} not added by the owner")
    if renamed_by_other(client, settings, issue.number):
        return TrustCheck(False, "title changed by someone other than the owner")
    return TrustCheck(True, "opened and labelled by the owner")


def check_local_ok(client: httpx.Client, settings: DispatchSettings, issue: Issue) -> TrustCheck:
    """``dispatch:local-ok`` is on the issue and the owner added it (step 4b)."""
    if LOCAL_OK_LABEL not in issue.labels:
        return TrustCheck(False, f"no {LOCAL_OK_LABEL} label")
    adder = label_adder(client, settings, issue.number, LOCAL_OK_LABEL)
    if not is_owner(adder, settings):
        return TrustCheck(False, f"{LOCAL_OK_LABEL} not added by the owner")
    return TrustCheck(True, f"{LOCAL_OK_LABEL} added by the owner")


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.post(path, json=body)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from None
    data = response.json()
    if not isinstance(data, dict | list):
        raise GitHubError("GitHub answered something unexpected")
    return data if isinstance(data, dict) else {"items": data}


def open_pull_request(
    client: httpx.Client, settings: DispatchSettings, branch: str, title: str, body: str
) -> int:
    """Open a pull request from ``branch`` into main. Returns its number. Never merges."""
    if not branch.startswith("dispatch/"):
        raise GitHubError("refused: dispatcher branches start with dispatch/")
    data = _post(
        client,
        f"{_repo_path(settings)}/pulls",
        {"title": title, "head": branch, "base": "main", "body": body, "draft": False},
    )
    number = data.get("number")
    if not isinstance(number, int):
        raise GitHubError("GitHub answered a pull request without a number")
    return number


def add_label(client: httpx.Client, settings: DispatchSettings, number: int, label: str) -> None:
    """Add one dispatcher label to an issue or pull request."""
    if label not in {LOCAL_PR_LABEL, CLAUDE_LABEL}:
        raise GitHubError(f"refused: the dispatcher never adds {label}")
    _post(client, f"{_repo_path(settings)}/issues/{int(number)}/labels", {"labels": [label]})
