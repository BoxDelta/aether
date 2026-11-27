"""The always-Claude list (ADR-030 §Always-Claude list).

This file is itself on the list. A match routes to ``claude`` with no model call.
Text matching is deliberately broad: a false match costs a Claude session, a
missed one could let a local model touch a guarded area.
"""

import re
from collections.abc import Iterable

# Paths from ADR-030. Directories end in '/'. ``docs/05_`` covers docs/05_*.
ALWAYS_CLAUDE_PATHS: tuple[str, ...] = (
    ".github/",
    ".claude/",
    "CLAUDE.md",
    ".pre-commit-config.yaml",
    "scripts/safemerge.fish",
    ".env.example",
    "pyproject.toml",
    "uv.lock",
    "router/",
    "memory/",
    "migrations/",
    "docs/05_",
    "docs/adr/",
    "dispatch/",
)
CORE_FILES: tuple[str, ...] = (
    "auth.py",
    "config.py",
    "chat.py",
    "memory_api.py",
    "identity.py",
    "db.py",
    "db_admin.py",
    "backup.py",
)
ALWAYS_CLAUDE_LABELS: tuple[str, ...] = ("model:opus", "security")
# Not a separate rule: the Dependabot label is one way an issue says it adds a dependency.
DEPENDENCY_LABELS: tuple[str, ...] = ("dependencies",)

_EDGE = r"(?<![\w.-])"  # not part of a longer name ("api.github.com", "test_db.py")
_END = r"(?![\w-])"


def _path_pattern(path: str) -> re.Pattern[str]:
    if path == "docs/05_":
        body = r"docs/05(?!\d)"  # issue text says "docs/05 §2" as often as the file name
    elif path.startswith(".") and path.endswith("/"):
        body = re.escape(path[:-1]) + r"(?:/|" + _END + ")"  # ".github" alone counts too
    elif path.endswith("/"):
        body = re.escape(path)
    else:
        body = re.escape(path) + _END
    return re.compile(_EDGE + body, re.IGNORECASE)


_PATH_PATTERNS = [(path, _path_pattern(path)) for path in ALWAYS_CLAUDE_PATHS]
_CORE_PATTERNS = [
    (
        f"core/{name}",
        re.compile(
            rf"{_EDGE}core/{re.escape(name)}{_END}|(?<![\w./-]){re.escape(name)}{_END}",
            re.IGNORECASE,
        ),
    )
    for name in CORE_FILES
]
_DEPENDENCY_TEXT = re.compile(
    r"\b(?:add|adds|adding|new|install|installs|introduce|introduces|bump|upgrade)\b"
    r"[^.\n]{0,40}\b(?:dependency|dependencies|package|library)\b"
    r"|\buv add\b|\bpip install\b",
    re.IGNORECASE,
)
_ADR_TEXT = re.compile(
    r"\b(?:new|needs?|write|add|propose|record)\s+(?:an?\s+)?ADR\b"
    r"|\bADR\s+(?:needed|required)\b",
    re.IGNORECASE,
)


def path_rule(path: str) -> str | None:
    """The rule a changed file path matches, or None. For diffs (step 4b)."""
    clean = path.removeprefix("./")
    for listed in ALWAYS_CLAUDE_PATHS:
        if listed.endswith("/") or listed == "docs/05_":
            if clean.startswith(listed):
                return f"path {listed}"
        elif clean == listed:
            return f"path {listed}"
    for name in CORE_FILES:
        if clean == f"core/{name}":
            return f"path core/{name}"
    return None


def label_rule(labels: Iterable[str]) -> str | None:
    names = {label.lower() for label in labels}
    for label in ALWAYS_CLAUDE_LABELS:
        if label in names:
            return f"label {label}"
    for label in DEPENDENCY_LABELS:
        if label in names:
            return f"adds a dependency (label {label})"
    return None


def text_rule(text: str) -> str | None:
    """The rule a title or body matches by naming a listed path or keyword."""
    for path, pattern in _PATH_PATTERNS:
        if pattern.search(text):
            return f"mentions {path}"
    for path, pattern in _CORE_PATTERNS:
        if pattern.search(text):
            return f"mentions {path}"
    if _DEPENDENCY_TEXT.search(text):
        return "adds a dependency"
    if _ADR_TEXT.search(text):
        return "needs an ADR"
    return None


def issue_rule(title: str, body: str, labels: Iterable[str]) -> str | None:
    """First always-Claude rule the issue matches, checked before any model call."""
    return label_rule(labels) or text_rule(f"{title}\n{body}")
