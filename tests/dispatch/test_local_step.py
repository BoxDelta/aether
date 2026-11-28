"""The run's local step (ADR-030 step 4b): what each outcome records, the checks
that run again before acting, and the GitHub writes it makes."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from dispatch import __main__ as cli
from dispatch import brief, github, local, notify, state
from dispatch.github import Issue
from tests.dispatch.helpers import OWNER, FakeGitHub, issue_json, make_settings, owner_labels

NOW = datetime(2026, 11, 27, 12, 0, 0)


@dataclass
class FakeWorker:
    outcomes: list[local.Outcome]
    tried: list[int] = field(default_factory=list)

    def attempt(self, issue: Issue, open_pr: object) -> local.Outcome:
        self.tried.append(issue.number)
        outcome = self.outcomes.pop(0)
        if outcome.kind == "opened":
            assert callable(open_pr)
            return local.Outcome("opened", outcome.reason, open_pr(f"dispatch/{issue.number}-x"))
        return outcome


@dataclass
class FakeWriter:
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(201, json={"number": 300})

    def calls(self) -> list[tuple[str, object]]:
        return [(r.url.path, json.loads(r.content)) for r in self.requests]


def _issue(number: int, labels: tuple[str, ...] | None = None) -> Issue:
    kwargs = {} if labels is None else {"labels": labels}
    item = github._to_issue(issue_json(number, **kwargs))  # type: ignore[arg-type]
    assert item is not None
    return item


def _step(
    tmp_path: Path, worker: FakeWorker, issues: list[Issue]
) -> tuple[cli.LocalStep, FakeWriter, FakeGitHub]:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(
        issues=[issue_json(i.number, labels=i.labels) for i in issues],
        events={i.number: owner_labels() for i in issues},
    )
    writer = FakeWriter()
    run = cli.BriefRun(settings, NOW, brief.load_template(), tmp_path / "s.json", tmp_path / "b")
    step = cli.LocalStep(
        fake.client(settings),
        github.make_write_client(settings, httpx.MockTransport(writer.handler)),
        lambda issue: "notes",
        run,
        worker,
    )
    return step, writer, fake


def test_opened_records_the_pr_and_labels_it(tmp_path: Path) -> None:
    issue = _issue(5)
    step, writer, _ = _step(tmp_path, FakeWorker([local.Outcome("opened", "1 files")]), [issue])
    st = state.State(local={5})
    rows = step.process(st, {5: issue})
    assert st.opened == {5: 300} and st.local == set()
    assert rows[0].reason.startswith("PR #300 opened")
    pr, label = writer.calls()
    assert pr[0] == f"/repos/{OWNER}/aether/pulls"
    assert pr[1]["head"] == "dispatch/5-x" and pr[1]["base"] == "main"  # type: ignore[index]
    assert "Refs #5" in pr[1]["body"]  # type: ignore[index]
    assert label == (f"/repos/{OWNER}/aether/issues/300/labels", {"labels": ["dispatch:local"]})
    assert notify.pr_opened(5, 300) == "#5 local PR #300 opened"


def test_blocked_diff_becomes_a_brief(tmp_path: Path) -> None:
    issue = _issue(6)
    outcome = local.Outcome("blocked", "path check: path router/")
    step, writer, _ = _step(tmp_path, FakeWorker([outcome]), [issue])
    st = state.State(local={6})
    step.process(st, {6: issue})
    assert st.briefed == {6} and st.local == set()
    text = (tmp_path / "b" / "6.md").read_text()
    assert "Sorted to Claude (diff): path check: path router/" in text
    assert writer.calls() == [
        (f"/repos/{OWNER}/aether/issues/6/labels", {"labels": ["dispatch:claude"]})
    ]


def test_second_failed_try_becomes_a_brief(tmp_path: Path) -> None:
    issue = _issue(7)
    failed = local.Outcome("failed", "tests failed: 1 failed")
    worker = FakeWorker([failed, failed])
    step, writer, _ = _step(tmp_path, worker, [issue])
    st = state.State(local={7})
    step.process(st, {7: issue})
    assert st.tries == {7: 1} and st.local == {7} and writer.calls() == []
    step.process(st, {7: issue})
    assert st.tries == {7: 2} and st.briefed == {7} and st.local == set()
    assert "2 failed tries" in (tmp_path / "b" / "7.md").read_text()
    assert len(writer.calls()) == 1


def test_rules_and_labels_run_again_before_acting(tmp_path: Path) -> None:
    # Labelled security after it was sorted local: no try, a brief instead.
    issue = _issue(8, (github.AUTO_LABEL, github.LOCAL_OK_LABEL, "security"))
    worker = FakeWorker([])
    step, _, _ = _step(tmp_path, worker, [issue])
    st = state.State(local={8})
    rows = step.process(st, {8: issue})
    assert worker.tried == []
    assert st.briefed == {8}
    assert rows[0].reason == "label security"


def test_local_ok_removed_means_a_brief(tmp_path: Path) -> None:
    issue = _issue(9, (github.AUTO_LABEL,))
    worker = FakeWorker([])
    step, _, _ = _step(tmp_path, worker, [issue])
    st = state.State(local={9})
    step.process(st, {9: issue})
    assert worker.tried == [] and st.briefed == {9}


def test_closed_issue_is_dropped(tmp_path: Path) -> None:
    worker = FakeWorker([])
    step, _, _ = _step(tmp_path, worker, [])
    st = state.State(local={10})
    step.process(st, {})
    assert st.local == set() and worker.tried == []


def test_one_try_per_run(tmp_path: Path) -> None:
    issues = [_issue(11), _issue(12)]
    worker = FakeWorker([local.Outcome("failed", "x"), local.Outcome("failed", "x")])
    step, _, _ = _step(tmp_path, worker, issues)
    step.process(state.State(local={11, 12}), {i.number: i for i in issues})
    assert worker.tried == [11]


def test_announce_posts_pr_numbers_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["content"])
        return httpx.Response(204)

    st = state.State(briefed={1}, opened={2: 40})
    with notify.make_client(httpx.MockTransport(handler)) as client:
        assert cli.announce(st, settings, client, tmp_path / "s.json") == 2
    assert sent == ["#1 brief ready", "#2 local PR #40 opened"]
