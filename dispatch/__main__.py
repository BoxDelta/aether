"""``uv run python -m dispatch triage [--issue N]`` and ``... eval <set.yaml>``.

Dry run (ADR-030 step 4a): prints and records routes, changes nothing on GitHub.
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

from dispatch import classify, github
from dispatch.config import ConfigError, DispatchSettings, load_settings

REPORTS_DIR = Path(".dispatch/reports")


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


def triage_rows(
    gh: httpx.Client, settings: DispatchSettings, triage: Triage, number: int | None
) -> list[Row]:
    if number is None:
        issues = github.list_auto_issues(gh, settings)
    else:
        one = github.get_issue(gh, settings, number)
        if one is None:
            return [Row(number, "-", "trust", "pull request, not an issue")]
        issues = [one]
    rows = []
    for issue in issues:
        trust = github.check_trust(gh, settings, issue)  # again, right before the step
        if not trust.ok:
            rows.append(Row(issue.number, "-", "trust", trust.reason))
            continue
        decision = triage(issue)
        rows.append(Row(issue.number, decision.route, decision.step, decision.reason))
    return rows


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


def load_eval_set(path: Path) -> EvalSet:
    return EvalSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dispatch")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="dry-run sort of dispatch:auto issues")
    triage.add_argument("--issue", type=int, help="sort one issue (both owner checks apply)")
    evaluate = sub.add_parser("eval", help="sort a fixed set of past issues")
    evaluate.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    with github.make_client(settings) as gh, classify.make_client(settings) as llm:

        def triage(issue: github.Issue) -> classify.Decision:
            return classify.triage(issue, llm)

        try:
            if args.command == "eval":
                eval_set = load_eval_set(args.path)
                lines, matches = eval_rows(gh, settings, triage, eval_set)
                print("\n".join(lines))
                return 0 if matches == len(eval_set.issues) else 1
            table = format_table(triage_rows(gh, settings, triage, args.issue))
        except github.GitHubError as exc:
            print(f"github error: {exc}", file=sys.stderr)
            return 2
    print(table)
    print(f"\nreport: {write_report(table, datetime.now().astimezone())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
