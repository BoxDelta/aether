"""The triage and eval commands, end to end against fakes (no network)."""

from datetime import datetime
from pathlib import Path

import pytest

from dispatch import __main__ as cli
from dispatch import classify
from dispatch.github import Issue
from tests.dispatch.helpers import (
    FakeGitHub,
    issue_json,
    labeled,
    make_settings,
    owner_labels,
)

FIXTURE = Path(__file__).parent / "fixtures" / "triage_set.yaml"


def _always_local(issue: Issue) -> classify.Decision:
    return classify.Decision("local", "model", f"fake for #{issue.number}")


def test_triage_checks_trust_per_issue(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(
        issues=[issue_json(1), issue_json(2, author="mallory"), issue_json(3)],
        events={1: owner_labels(), 2: owner_labels(), 3: [labeled("mallory")]},
    )
    with fake.client(settings) as gh:
        rows = cli.triage_rows(gh, settings, _always_local, None)
    assert [(r.issue, r.route, r.step) for r in rows] == [
        (1, "local", "model"),
        (2, "-", "trust"),
        (3, "-", "trust"),
    ]
    assert all(req.method == "GET" for req in fake.requests)


def test_triage_one_issue_still_checks_both(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(issues=[issue_json(5)], events={5: [labeled("mallory")]})
    with fake.client(settings) as gh:
        rows = cli.triage_rows(gh, settings, _always_local, 5)
    assert rows == [cli.Row(5, "-", "trust", "dispatch:auto not added by the owner")]


def test_triage_one_pull_request_is_skipped(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(issues=[issue_json(5, pull_request=True)])
    with fake.client(settings) as gh:
        assert cli.triage_rows(gh, settings, _always_local, 5)[0].step == "trust"


def test_eval_skips_label_check_not_author_check(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeGitHub(issues=[issue_json(1, labels=()), issue_json(2, author="mallory")])
    eval_set = cli.EvalSet.model_validate(
        {
            "issues": [
                {"issue": 1, "expected": "local", "deciding_files": ["docs/x.md"]},
                {"issue": 2, "expected": "local", "deciding_files": ["docs/y.md"]},
            ]
        }
    )
    with fake.client(settings) as gh:
        lines, matches = cli.eval_rows(gh, settings, _always_local, eval_set)
    assert matches == 1
    assert "MATCH" in lines[0]
    assert "skipped (not opened by the owner)" in lines[1]
    assert lines[-1] == "matches: 1/2"
    assert not any(req.url.path.endswith("/timeline") for req in fake.requests)


def test_fixture_is_valid() -> None:
    eval_set = cli.load_eval_set(FIXTURE)
    assert len(eval_set.issues) >= 10
    assert {item.expected for item in eval_set.issues} == {"local", "claude"}


def test_report_table_and_file(tmp_path: Path) -> None:
    table = cli.format_table([cli.Row(4, "claude", "rule", "mentions router/ | x\ny")])
    assert table.splitlines()[2] == "| #4 | claude | rule | mentions router/ / x y |"
    path = cli.write_report(table, datetime(2026, 11, 26, 9, 30), tmp_path / "reports")
    assert path.name == "2026-11-26.md"
    assert "## Triage dry run 09:30:00" in path.read_text("utf-8")


def test_main_reports_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISPATCH_ENV_FILE", str(tmp_path / "missing.env"))
    assert cli.main(["triage"]) == 2
    assert "config error" in capsys.readouterr().err


def test_reports_dir_is_at_repo_root() -> None:
    root = Path(__file__).resolve().parents[2]
    assert cli.REPORTS_DIR.relative_to(root) == Path(".dispatch/reports")
