"""The fixed path check on a local diff, before any local pull request opens (step 4b).

Same always-Claude list as triage (``rules.py``). A diff is blocked, and its issue
turned into a Claude brief, if it:

- touches a listed path, auth or permission code, or a dependency file;
- changes lines about database roles, grants or credentials, or a security review;
- has a binary file, a symlink, a submodule, a mode change, or a path git had to quote;
- is empty, or larger than ``MAX_LINES`` changed lines across ``MAX_FILES`` files.

The check reads the diff text only. It never runs anything from the diff.
"""

import re
from dataclasses import dataclass

from dispatch import rules

MAX_LINES = 200
MAX_FILES = 5

_HEADER = re.compile(r"^diff --git (\S+) (\S+)$")
_RENAME = re.compile(r"^(?:rename|copy) (?:from|to) (.+)$")
_SIDE = re.compile(r"^(?:---|\+\+\+) (.+)$")
_RISKY_MODES = re.compile(r"^(?:new file mode|deleted file mode|old mode|new mode) (\d+)$")
_SAFE_MODE = "100644"


@dataclass(frozen=True, slots=True)
class DiffCheck:
    ok: bool
    reason: str
    files: tuple[str, ...]
    changed_lines: int


def _strip_side(path: str) -> str | None:
    if path == "/dev/null":
        return None
    if path.startswith('"'):
        return path  # quoted by git: odd characters, blocked below
    return path[2:] if path[:2] in {"a/", "b/"} else path


def _header_paths(raw: str, problems: list[str]) -> list[str]:
    match = _HEADER.match(raw)
    if match is None:
        problems.append("unreadable diff header (quoted or odd path)")
        return []
    return [p for side in match.groups() if (p := _strip_side(side)) is not None]


def _meta(raw: str, paths: list[str], problems: list[str]) -> None:
    """A line between a ``diff --git`` header and the first hunk."""
    if match := _RENAME.match(raw):
        paths.append(match.group(1))
    elif match := _SIDE.match(raw):
        stripped = _strip_side(match.group(1))
        if stripped is not None:
            paths.append(stripped)
    elif raw.startswith(("Binary files ", "GIT binary patch")):
        problems.append("binary file")
    elif (mode := _RISKY_MODES.match(raw)) and mode.group(1) != _SAFE_MODE:
        problems.append(f"file mode change ({raw})")


def parse(diff: str) -> tuple[list[str], list[str], list[str]]:
    """Changed paths, changed lines (without the +/-), and structural problems."""
    paths: list[str] = []
    lines: list[str] = []
    problems: list[str] = []
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
            paths += _header_paths(raw, problems)
        elif raw.startswith("@@"):
            in_hunk = True
        elif not in_hunk:
            _meta(raw, paths, problems)
        elif raw.startswith(("+", "-")):
            lines.append(raw[1:])
    return paths, lines, problems


def _path_problem(path: str) -> str | None:
    if path.startswith('"') or "\\" in path or any(ord(c) < 32 for c in path):
        return "path git had to quote"
    if path.startswith("/") or ".." in path.split("/"):
        return "path outside the worktree"
    return rules.path_rule(path)


def _block_reason(  # noqa: PLR0911 - one return per check, in a fixed order
    files: tuple[str, ...], lines: list[str], problems: list[str]
) -> str | None:
    """The first reason to block, in a fixed order, or None."""
    if not files:
        return "no change"
    for path in files:
        problem = _path_problem(path)
        if problem is not None:
            return problem
    if problems:
        return problems[0]
    rule = rules.content_rule("\n".join(lines))
    if rule is not None:
        return f"changed lines: {rule}"
    if len(files) > MAX_FILES:
        return f"{len(files)} files, more than {MAX_FILES}"
    if len(lines) > MAX_LINES:
        return f"{len(lines)} changed lines, more than {MAX_LINES}"
    return None


def check(diff: str, extra_paths: tuple[str, ...] = ()) -> DiffCheck:
    """``extra_paths``: names from ``git diff --name-only -z``, checked as well, so a
    path the text parser misread is still seen."""
    parsed_paths, lines, problems = parse(diff)
    files = tuple(dict.fromkeys([*parsed_paths, *extra_paths]))
    reason = _block_reason(files, lines, problems)
    if reason is not None:
        return DiffCheck(False, reason, files, len(lines))
    return DiffCheck(True, f"{len(files)} files, {len(lines)} changed lines", files, len(lines))
