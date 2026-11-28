"""``uv run python -m dispatch run|triage [--issue N]`` and ``... eval <set.yaml>``.

- ``triage``: sort and print only (the step 4a dry run, kept for checks).
- ``run``: sort, then the Claude path (step 4c): a brief for each ``claude`` issue
  and "#<n> brief ready" on Discord; then the local path (step 4b) for at most one
  issue sorted ``local``: Aider, the path check, tests, a pull request.

Only the local path writes to GitHub: it pushes a ``dispatch/`` branch, opens a
pull request and adds ``dispatch:local`` or ``dispatch:claude`` labels.
"""

import argparse
import functools
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from dispatch import brief, classify, github, local, notify, rules, state
from dispatch.config import ConfigError, DispatchSettings, load_settings

REPORTS_DIR = Path(__file__).resolve().parents[1] / ".dispatch" / "reports"


@dataclass(frozen=True, slots=True)
class Row:
    issue: int
    route: str
    step: str
    reason: str


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue: int = Field(gt=0)
    expected: Literal["local", "claude"]
    deciding_files: list[str] = Field(min_length=1)
    deciding_label: str | None = None


class EvalSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issues: list[EvalItem] = Field(min_length=1)


def format_table(rows: Sequence[Row]) -> str:
    """A Markdown table, readable as plain text too."""
    lines = ["| issue | route | step | reason |", "| --- | --- | --- | --- |"]
    lines += [
        f"| #{r.issue} | {r.route} | {r.step} | {classify.clean_reason(r.reason)} |" for r in rows
    ]
    return "\n".join(lines)


def write_report(table: str, now: datetime, reports_dir: Path = REPORTS_DIR) -> Path:
    reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    reports_dir.chmod(0o700)
    path = reports_dir / f"{now.date().isoformat()}.md"
    # Mode 600 whatever the umask: reports hold model reasons (manual runs too).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as report:
        report.write(f"## Triage dry run {now.strftime('%H:%M:%S')}\n\n{table}\n\n")
    return path


Triage = Callable[[github.Issue], classify.Decision]


@dataclass(frozen=True, slots=True)
class Sorted:
    row: Row
    issue: github.Issue | None  # None when the trust check failed
    decision: classify.Decision | None


def sort_issues(
    gh: httpx.Client,
    settings: DispatchSettings,
    triage: Triage,
    number: int | None,
    skip: frozenset[int] = frozenset(),
) -> list[Sorted]:
    """Trusted issues, sorted. Issues in ``skip`` (already handled) are left out
    before the trust check, the rules and the model. Without an owner-added
    ``dispatch:local-ok`` label an issue goes to Claude with no model call."""
    if number is None:
        issues = github.list_auto_issues(gh, settings)
    else:
        one = github.get_issue(gh, settings, number)
        if one is None:
            return [Sorted(Row(number, "-", "trust", "pull request, not an issue"), None, None)]
        issues = [one]
    out = []
    for issue in issues:
        if issue.number in skip:
            continue
        trust = github.check_trust(gh, settings, issue)  # again, right before the step
        if not trust.ok:
            out.append(Sorted(Row(issue.number, "-", "trust", trust.reason), None, None))
            continue
        decision = _sort_one(gh, settings, triage, issue)
        row = Row(issue.number, decision.route, decision.step, decision.reason)
        out.append(Sorted(row, issue, decision))
    return out


def _sort_one(
    gh: httpx.Client, settings: DispatchSettings, triage: Triage, issue: github.Issue
) -> classify.Decision:
    matched = rules.issue_rule(issue.title, issue.body, issue.labels)
    if matched is not None:
        return classify.Decision("claude", "rule", matched)
    local_ok = github.check_local_ok(gh, settings, issue)
    if not local_ok.ok:
        return classify.Decision("claude", "label", local_ok.reason)
    return triage(issue)


def triage_rows(
    gh: httpx.Client,
    settings: DispatchSettings,
    triage: Triage,
    number: int | None,
    skip: frozenset[int] = frozenset(),
) -> list[Row]:
    return [s.row for s in sort_issues(gh, settings, triage, number, skip)]


def eval_rows(
    gh: httpx.Client, settings: DispatchSettings, triage: Triage, eval_set: EvalSet
) -> tuple[list[str], int]:
    """Rules + model on past issues. Skips only the label check, never the author check."""
    lines, matches = [], 0
    for item in eval_set.issues:
        issue = github.get_issue(gh, settings, item.issue)
        if issue is None:
            lines.append(f"#{item.issue}: skipped (pull request)")
            continue
        author = github.check_author(issue, settings)
        if not author.ok:
            lines.append(f"#{item.issue}: skipped ({author.reason})")
            continue
        decision = triage(issue)
        ok = decision.route == item.expected
        matches += ok
        lines.append(
            f"#{item.issue}: expected {item.expected}, got {decision.route} "
            f"({decision.step}: {classify.clean_reason(decision.reason)}) "
            f"{'MATCH' if ok else 'MISS'}"
        )
    lines.append(f"matches: {matches}/{len(eval_set.issues)}")
    return lines, matches


Notes = Callable[[github.Issue], str]
Trusted = Callable[[github.Issue], bool]


def _always(_: github.Issue) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class BriefRun:
    settings: DispatchSettings
    now: datetime
    template: str
    state_file: Path = state.STATE_FILE
    briefs_dir: Path = brief.BRIEFS_DIR
    trusted: Trusted = _always  # the trust check again, right before the brief step


def write_briefs(
    items: Sequence[Sorted], notes: Notes, st: state.State, run: BriefRun
) -> list[int]:
    """A brief for each ``claude`` issue; ``local`` issues are recorded for step 4b.
    State is saved after each issue, so a crash never repeats finished work."""
    written = []
    for item in items:
        if item.issue is None or item.decision is None:
            continue  # trust failed: checked again next run
        number = item.issue.number
        if item.decision.route == "local":
            st.local.add(number)
        elif not run.trusted(item.issue):
            continue  # trust changed since sorting: nothing recorded, checked next run
        else:
            content = brief.Content(
                item.issue,
                notify_url(run.settings, number),
                item.decision,
                notes(item.issue),
                run.now,
            )
            brief.write(number, brief.build(content, run.template), run.briefs_dir)
            st.briefed.add(number)
            written.append(number)
        state.save(st, run.state_file)
    return written


def announce(
    st: state.State,
    settings: DispatchSettings,
    discord: httpx.Client,
    state_file: Path = state.STATE_FILE,
) -> int:
    """One post per brief or local pull request; recorded only when Discord accepted.
    Posts that failed earlier are tried again here, without a model call."""
    sent = 0
    pending = [(n, notify.brief_ready(n)) for n in sorted(st.briefed - st.announced)]
    pending += [
        (n, notify.pr_opened(n, pr)) for n, pr in sorted(st.opened.items()) if n not in st.announced
    ]
    for number, message in pending:
        if notify.deliver(discord, settings, message):
            st.announced.add(number)
            state.save(st, state_file)
            sent += 1
    return sent


MAX_LOCAL_PER_RUN = 1
MAX_TRIES = 2


class Attempt(Protocol):
    def attempt(self, issue: github.Issue, open_pr: Callable[[str], int]) -> local.Outcome: ...


@dataclass
class LocalStep:
    """The local path for issues sorted ``local`` (step 4b)."""

    gh: httpx.Client  # read client
    writer: httpx.Client  # write client: open a pull request, add labels
    notes: Notes
    run: BriefRun
    worker: Attempt

    def to_brief(self, st: state.State, issue: github.Issue, decision: classify.Decision) -> None:
        number = issue.number
        content = brief.Content(
            issue, notify_url(self.run.settings, number), decision, self.notes(issue), self.run.now
        )
        brief.write(number, brief.build(content, self.run.template), self.run.briefs_dir)
        st.local.discard(number)
        st.briefed.add(number)
        state.save(st, self.run.state_file)

    def open_pr(self, issue: github.Issue, branch: str) -> int:
        settings = self.run.settings
        title = f"dispatch: #{issue.number} {classify.clean_reason(issue.title)}"[:120]
        body = (
            f"Refs #{issue.number}\n\nOpened by the dispatcher's local path (ADR-030 step 4b): "
            "Aider with the local model. The path check passed, and ruff, mypy and pytest "
            "passed in the no-network container. Review as usual; the owner merges."
        )
        pr = github.open_pull_request(self.writer, settings, branch, title, body)
        github.add_label(self.writer, settings, pr, github.LOCAL_PR_LABEL)
        return pr

    def process(self, st: state.State, open_issues: Mapping[int, github.Issue]) -> list[Row]:
        """At most ``MAX_LOCAL_PER_RUN`` tries. Every check runs again before acting."""
        settings, rows = self.run.settings, []
        for number in sorted(st.local)[:MAX_LOCAL_PER_RUN]:
            issue = open_issues.get(number)
            if issue is None:  # closed, or dispatch:auto removed
                st.local.discard(number)
                state.save(st, self.run.state_file)
                continue
            if not github.check_trust(self.gh, settings, issue).ok:
                continue  # checked again next run
            recheck = _sort_one(self.gh, settings, _local_again, issue)
            if recheck.route != "local":
                self.to_brief(st, issue, recheck)
                rows.append(Row(number, "claude", recheck.step, recheck.reason))
                continue
            outcome = self.worker.attempt(issue, functools.partial(self.open_pr, issue))
            rows.append(self._record(st, issue, outcome))
        return rows

    def _record(self, st: state.State, issue: github.Issue, outcome: local.Outcome) -> Row:
        number, settings = issue.number, self.run.settings
        if outcome.kind == "opened" and outcome.pr is not None:
            st.local.discard(number)
            st.opened[number] = outcome.pr
            state.save(st, self.run.state_file)
            return Row(number, "local", "local", f"PR #{outcome.pr} opened ({outcome.reason})")
        if outcome.kind == "failed":
            st.tries[number] = st.tries.get(number, 0) + 1
            state.save(st, self.run.state_file)
            if st.tries[number] < MAX_TRIES:
                return Row(
                    number, "local", "local", f"try {st.tries[number]} failed: {outcome.reason}"
                )
            decision = classify.Decision(
                "claude", "local", f"{MAX_TRIES} failed tries: {outcome.reason}"
            )
        else:
            decision = classify.Decision("claude", "diff", outcome.reason)
        self.to_brief(st, issue, decision)
        github.add_label(self.writer, settings, number, github.CLAUDE_LABEL)
        return Row(number, "claude", decision.step, decision.reason)


def _local_again(_: github.Issue) -> classify.Decision:
    """The model already said local; the re-check repeats only the rules and labels."""
    return classify.Decision("local", "model", "sorted local earlier")


def notify_url(settings: DispatchSettings, number: int) -> str:
    return f"https://github.com/{settings.dispatch_owner}/{settings.dispatch_repo}/issues/{number}"


def load_eval_set(path: Path) -> EvalSet:
    return EvalSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dispatch")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="sort dispatch:auto issues and print only")
    triage.add_argument("--issue", type=int, help="sort one issue (both owner checks apply)")
    run = sub.add_parser("run", help="sort new issues, write Claude briefs, try the local path")
    run.add_argument("--issue", type=int, help="sort one issue (both owner checks apply)")
    run.add_argument("--no-local", action="store_true", help="skip the local path this run")
    evaluate = sub.add_parser("eval", help="sort a fixed set of past issues")
    evaluate.add_argument("path", type=Path)
    return parser


def _print_table(rows: Sequence[Row]) -> None:
    table = format_table(rows)
    print(table)
    print(f"\nreport: {write_report(table, datetime.now().astimezone())}")


def _run(
    gh: httpx.Client,
    settings: DispatchSettings,
    triage: Triage,
    notes: Notes,
    args: argparse.Namespace,
) -> int:
    try:
        with state.run_lock():
            st = state.load()  # before the trust check, the rules and the model
            items = sort_issues(gh, settings, triage, args.issue, st.handled())
            if items:
                _print_table([item.row for item in items])
                now = datetime.now().astimezone()
                run = BriefRun(
                    settings,
                    now,
                    brief.load_template(),
                    trusted=lambda issue: github.check_trust(gh, settings, issue).ok,
                )
                written = write_briefs(items, notes, st, run)
                print(f"briefs: wrote {len(written)}")
            else:
                print("nothing new")
            if st.local and not args.no_local:
                now = datetime.now().astimezone()
                run = BriefRun(settings, now, brief.load_template())
                open_issues = {i.number: i for i in github.list_auto_issues(gh, settings)}
                with github.make_write_client(settings) as writer:
                    step = LocalStep(gh, writer, notes, run, local.LocalPath(settings))
                    rows = step.process(st, open_issues)
                if rows:
                    _print_table(rows)
            if settings.dispatch_discord_webhook is None:
                print("discord: no webhook configured, nothing posted")
                return 0
            with notify.make_client() as discord:
                print(f"discord: posted {announce(st, settings, discord)}")
            return 0
    except state.Busy:
        print("another run is in progress")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    with github.make_client(settings) as gh, classify.make_client(settings) as llm:
        gate = classify.GpuGate()  # one per run

        def triage(issue: github.Issue) -> classify.Decision:
            return classify.triage(issue, llm, gate)

        def notes(issue: github.Issue) -> str:
            return brief.ask_notes(llm, issue, gate)

        try:
            if args.command == "eval":
                eval_set = load_eval_set(args.path)
                lines, matches = eval_rows(gh, settings, triage, eval_set)
                print("\n".join(lines))
                return 0 if matches == len(eval_set.issues) else 1
            if args.command == "run":
                return _run(gh, settings, triage, notes, args)
            _print_table(triage_rows(gh, settings, triage, args.issue))
            return 0
        except github.GitHubError as exc:
            print(f"github error: {exc}", file=sys.stderr)
            return 2
        except state.StateError as exc:
            print(f"state error: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
