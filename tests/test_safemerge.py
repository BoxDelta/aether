"""scripts/safemerge.fish (#60): syntax and logic, with gh and git stubbed.

The repo copy is a backup of the owner's live function, never sourced from here
in real use. These tests source it in a throwaway fish (``--no-config``) with
stub ``gh``/``git`` first on PATH and a temporary HOME, so nothing can reach
GitHub or the real checkout. The stubs only record what would have run.

Skipped where fish isn't installed (Sentinel's runner); run locally.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "safemerge.fish"
FISH = shutil.which("fish")
HEAD = "0123456789abcdef0123456789abcdef01234567"

pytestmark = pytest.mark.skipif(FISH is None, reason="fish not installed")

GH_STUB = """#!/bin/sh
echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "pr view")
    if [ -n "$STUB_VIEW_FAIL" ]; then echo "GraphQL: Could not resolve" >&2; exit 1; fi
    printf '%s\\n' "$STUB_SHA"; exit 0 ;;
  "pr checks")
    if [ -n "$STUB_NO_CHECKS" ]; then
      # Real gh 2.104.0, observed on a PR with no checks (plain and --watch --fail-fast).
      echo "no checks reported on the 'feature' branch" >&2; exit 1
    fi
    exit "${STUB_CHECKS_EXIT:-0}" ;;
  "pr merge") exit 0 ;;
esac
exit 2
"""
GIT_STUB = """#!/bin/sh
echo "git $*" >> "$STUB_LOG"
if [ "$1 $2 $3" = "remote get-url origin" ]; then
  [ -n "$STUB_ORIGIN" ] || exit 2
  echo "$STUB_ORIGIN"
fi
exit 0
"""
ORIGIN = "git@github.com:BoxDelta/aether.git"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run(
    tmp_path: Path,
    *args: str,
    sha: str = HEAD,
    view_fail: bool = False,
    checks_exit: int = 0,
    no_checks: bool = False,
    origin: str = ORIGIN,
) -> tuple[int, str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _executable(bin_dir / "gh", GH_STUB)
    _executable(bin_dir / "git", GIT_STUB)
    home = tmp_path / "home"
    (home / "Projects" / "aether").mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "STUB_LOG": str(log),
        "STUB_SHA": sha,
        "STUB_CHECKS_EXIT": str(checks_exit),
        "STUB_ORIGIN": origin,
    }
    if no_checks:
        env["STUB_NO_CHECKS"] = "1"
    if view_fail:
        env["STUB_VIEW_FAIL"] = "1"
    quoted = " ".join(f"'{a}'" for a in args)
    proc = subprocess.run(  # noqa: S603 - fixed argv, stubbed tools
        [FISH or "fish", "--no-config", "-c", f"source {SCRIPT}; safemerge {quoted}"],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr, log.read_text().splitlines()


def merged(calls: list[str]) -> bool:
    return any(c.startswith("gh pr merge") for c in calls)


def test_syntax_is_valid() -> None:
    proc = subprocess.run(  # noqa: S603
        [FISH or "fish", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_reviewed_commit_matches_merges_that_exact_head(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55", HEAD[:12])

    assert code == 0
    assert calls == [
        "gh pr view 55 --repo BoxDelta/aether --json headRefOid --jq .headRefOid",
        "gh pr checks 55 --repo BoxDelta/aether --watch --fail-fast",
        f"gh pr merge 55 --repo BoxDelta/aether --merge --delete-branch --match-head-commit {HEAD}",
        "git remote get-url origin",
        "git switch main",
        "git pull",
    ]


def test_full_sha_matches(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55", HEAD)
    assert code == 0 and merged(calls)


def test_reviewed_commit_mismatch_does_not_merge(tmp_path: Path) -> None:
    code, out, calls = run(tmp_path, "55", "deadbeef")

    assert code == 1
    assert f"PR head is {HEAD}, not the reviewed commit deadbeef; not merging" in out
    assert calls == ["gh pr view 55 --repo BoxDelta/aether --json headRefOid --jq .headRefOid"]


def test_no_argument_prints_usage_and_calls_nothing(tmp_path: Path) -> None:
    code, out, calls = run(tmp_path)

    assert code == 1
    assert "usage: safemerge <PR number> [reviewed commit sha]" in out
    assert calls == []


def test_failing_checks_do_not_merge(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55", HEAD[:7], checks_exit=1)

    assert code == 1
    assert not merged(calls)
    assert not any(c.startswith("git") for c in calls)


def test_gh_view_failure_does_not_merge(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55", HEAD[:7], view_fail=True)

    assert code == 1
    assert calls == ["gh pr view 55 --repo BoxDelta/aether --json headRefOid --jq .headRefOid"]


def test_without_reviewed_commit_still_pins_the_viewed_head(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55")

    assert code == 0
    assert f"--match-head-commit {HEAD}" in calls[2]


# --- hardened reviewed-commit and head checks (#60 review) ---------------------------

VIEW = "gh pr view 55 --repo BoxDelta/aether --json headRefOid --jq .headRefOid"


@pytest.mark.parametrize(
    "reviewed",
    ["*", "0", "012345", HEAD[:12].upper(), "0123[0-9]", "?" * 7, HEAD + "0", "g123456"],
    ids=[
        "wildcard",
        "one-char",
        "six-chars",
        "uppercase",
        "bracket",
        "question",
        "41-chars",
        "non-hex",
    ],
)
def test_invalid_reviewed_commit_does_not_merge(tmp_path: Path, reviewed: str) -> None:
    code, out, calls = run(tmp_path, "55", reviewed)

    assert code == 1
    assert "reviewed commit must be 7-40 lowercase hex characters; not merging" in out
    assert calls == [VIEW]


def test_seven_char_prefix_is_the_minimum(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55", HEAD[:7])
    assert code == 0 and merged(calls)


@pytest.mark.parametrize(
    "sha", ["", "not-a-sha", HEAD[:39], HEAD.upper()], ids=["empty", "junk", "short", "upper"]
)
def test_unreadable_head_does_not_merge(tmp_path: Path, sha: str) -> None:
    for args in (("55",), ("55", HEAD[:7])):
        code, out, calls = run(tmp_path, *args, sha=sha)

        assert code == 1
        assert "could not read the PR head commit; not merging" in out
        assert calls == [VIEW]


def test_no_checks_reported_does_not_merge(tmp_path: Path) -> None:
    code, out, calls = run(tmp_path, "55", HEAD[:7], no_checks=True)

    assert code == 1
    assert "no checks reported" in out
    assert not merged(calls)


def test_outside_the_checkout_merges_but_does_not_touch_git(tmp_path: Path) -> None:
    code, out, calls = run(tmp_path, "55", HEAD[:7], origin="git@github.com:someone/other.git")

    assert code == 0 and merged(calls)
    assert "merged; in your aether checkout run: git switch main; and git pull" in out
    assert "git switch main" not in calls and "git pull" not in calls


def test_not_in_any_repo_merges_but_does_not_touch_git(tmp_path: Path) -> None:
    code, _, calls = run(tmp_path, "55", HEAD[:7], origin="")
    assert code == 0 and merged(calls)
    assert calls[-1] == "git remote get-url origin"


def test_script_contains_only_the_function() -> None:
    lines = [x for x in SCRIPT.read_text().splitlines() if x.strip()]
    assert lines[0].startswith("function safemerge ") and lines[-1] == "end"
    assert sum(1 for x in lines if x.startswith("function ")) == 1
    for home in ("~", "$HOME", "/home/"):
        assert home not in SCRIPT.read_text()
