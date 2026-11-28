"""``uv run python -m dispatch run|triage [--issue N]`` and ``... eval <set.yaml>``.

- ``triage``: sort and print only (the step 4a dry run, kept for checks).
- ``run``: sort, then the Claude path (step 4c): a brief for each ``claude`` issue
  and "#<n> brief ready" on Discord. Issues sorted ``local`` wait for step 4b.

Nothing here writes to GitHub.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from dispatch import brief, classify, github, notify, state
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
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{now.date().isoformat()}.md"
    with path.open("a", encoding="utf-8") as report:
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
    before the trust check, the rules and the model."""
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
        decision = triage(issue)
        row = Row(issue.number, decision.route, decision.step, decision.reason)
        out.append(Sorted(row, issue, decision))
    return out


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


@dataclass(frozen=True, slots=True)
class BriefRun:
    settings: DispatchSettings
    now: datetime
    template: str
    state_file: Path = state.STATE_FILE
    briefs_dir: Path = brief.BRIEFS_DIR


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
    """Post "#<n> brief ready" once per brief; record it only when Discord accepted.
    Briefs whose post failed earlier are tried again here, without a model call."""
    sent = 0
    for number in sorted(st.briefed - st.announced):
        if notify.deliver(discord, settings, notify.brief_ready(number)):
            st.announced.add(number)
            state.save(st, state_file)
            sent += 1
    return sent


def notify_url(settings: DispatchSettings, number: int) -> str:
    return f"https://github.com/{settings.dispatch_owner}/{settings.dispatch_repo}/issues/{number}"


def load_eval_set(path: Path) -> EvalSet:
    return EvalSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dispatch")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="sort dispatch:auto issues and print only")
    triage.add_argument("--issue", type=int, help="sort one issue (both owner checks apply)")
    run = sub.add_parser("run", help="sort new issues and write Claude briefs (step 4c)")
    run.add_argument("--issue", type=int, help="one issue (both owner checks apply)")
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
                run = BriefRun(settings, now, brief.load_template())
                written = write_briefs(items, notes, st, run)
                print(f"briefs: wrote {len(written)}")
            else:
                print("nothing new")
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
