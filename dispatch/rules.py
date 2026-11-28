"""The always-Claude list (ADR-030 §Always-Claude list): one list for everything.

The same list sorts issues (``issue_rule``, before any model call) and checks the
changed paths of a local diff (``path_rule``, step 4b). This file is itself on the
list. A match means ``claude``. Matching is deliberately broad: a false match costs
a Claude session, a missed one could let a local model touch a guarded area.

Sources, combined on 2026-11-27 (owner decision): the ADR-030 paths, auth code,
``tools/``, the files docs/05 names (a test keeps that in sync), the #133 gaps, and
the #61 rule (security reviews, database roles, grants and credentials).
"""

import re
import unicodedata
from collections.abc import Iterable

# Directories end in '/'. ``docs/05_`` covers docs/05_*.
ALWAYS_CLAUDE_PATHS: tuple[str, ...] = (
    # ADR-030
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
    # Tools (docs/05 §4) and the identity (docs/07)
    "tools/",
    "identity/",
    # Named in docs/05, or found missing in #133
    ".env",
    ".gitignore",
    "CONTRIBUTING.md",
    "docker-compose.yml",
    "compose.vault.yaml",
    "scripts/systemd/",
    "scripts/restore_check.sql",
    "scripts/aether-vault.fish",
    "scripts/identity_continuity.py",
    "tests/test_workflow_pins.py",
    "docs/runbooks/db-app-role.md",
    "docs/runbooks/db-backup-restore.md",
    "docs/runbooks/encrypted-volume.md",
)
CORE_FILES: tuple[str, ...] = (
    # ADR-030
    "auth.py",
    "config.py",
    "chat.py",
    "memory_api.py",
    "identity.py",
    "db.py",
    "db_admin.py",
    "backup.py",
    # docs/05 and #133
    "app.py",
    "events.py",
    "log.py",
    "ready.py",
    "request_context.py",
)
# Auth code anywhere: a path part (folder or file name) containing one of these.
AUTH_PARTS: tuple[str, ...] = ("auth", "login", "csrf", "passw", "secret", "credential")
ALWAYS_CLAUDE_LABELS: tuple[str, ...] = ("model:opus", "security", "dispatch:claude-only")
# Not a separate rule: the Dependabot label is one way an issue says it adds a dependency.
DEPENDENCY_LABELS: tuple[str, ...] = ("dependencies",)

_EDGE = r"(?<![\w.-])"  # not part of a longer name ("api.github.com", "test_db.py")
_END = r"(?![\w-])"


def _path_pattern(path: str) -> re.Pattern[str]:
    if path == "docs/05_":
        body = r"docs/05(?!\d)"  # issue text says "docs/05 §2" as often as the file name
    elif path == ".env":
        body = r"\.env(?![\w-])"  # also ".env.local"
    elif path.startswith(".") and path.endswith("/"):
        body = re.escape(path[:-1]) + r"(?:/|" + _END + ")"  # ".github" alone counts too
    elif path.endswith("/") and path.count("/") > 1:
        body = re.escape(path[:-1]) + r"(?:/|(?![\w.-]))"  # "docs/adr" without the slash too
    elif path.endswith("/"):
        body = re.escape(path)  # "router/", not the word "router"
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
# #61: an issue from a security review goes to Claude like a `security` label.
_SECURITY_REVIEW_TEXT = re.compile(
    r"\bsecurity[- ]review(?:er|s|ed)?\b|\bsecurity (?:audit|finding|issue)s?\b",
    re.IGNORECASE,
)
# #61: database roles, grants and credentials, test fixtures that create roles included.
# Also used on the changed lines of a local diff (step 4b).
SENSITIVE_CONTENT = re.compile(
    r"\b(?:create|alter|drop)\s+(?:role|user)\b"
    r"|\b(?:grant|grants|granted|revoke|revoked|privileges?|nologin|pg_roles|pg_authid)\b"
    r"|\b(?:db|database|postgres(?:ql)?|login|app|test|owner|admin)[ _-]roles?\b"
    r"|\baether_(?:app|admin|owner|migrat)\w*"
    r"|\b(?:credentials?|passwords?|passwd|secrets?|api[ _-]?keys?|bearer)\b"
    r"|\btokens? (?:hash|scope)s?\b",
    re.IGNORECASE,
)


def _auth_part(clean: str) -> str | None:
    for part in clean.lower().split("/"):
        for word in AUTH_PARTS:
            if word in part:
                return word
    return None


def path_rule(path: str) -> str | None:
    """The rule a changed file path matches, or None. For local diffs (step 4b)."""
    clean = path.removeprefix("./")
    for listed in ALWAYS_CLAUDE_PATHS:
        if listed.endswith("/") or listed == "docs/05_":
            if clean.startswith(listed):
                return f"path {listed}"
        elif listed == ".env":
            if clean.rsplit("/", 1)[-1].startswith(".env") and not clean.endswith(".example"):
                return "path .env"
        elif clean == listed:
            return f"path {listed}"
    for name in CORE_FILES:
        if clean == f"core/{name}":
            return f"path core/{name}"
    word = _auth_part(clean)
    if word is not None:
        return f"auth code (path has '{word}')"
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


def normalize(text: str) -> str:
    """NFKC, minus format characters, so zero-width or fullwidth tricks still match."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def content_rule(text: str) -> str | None:
    """Security-review origin, or database roles, grants or credentials (#61).

    Auth code is matched by path (``path_rule``, ``core/auth.py`` in text), not by the
    bare word, so a docs issue that mentions auth can still go local."""
    text = normalize(text)
    if _SECURITY_REVIEW_TEXT.search(text):
        return "from a security review"
    if SENSITIVE_CONTENT.search(text):
        return "database roles, grants or credentials"
    return None


def text_rule(text: str) -> str | None:
    """The rule a title or body matches by naming a listed path or keyword."""
    text = normalize(text)
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
    return content_rule(text)


def issue_rule(title: str, body: str, labels: Iterable[str]) -> str | None:
    """First always-Claude rule the issue matches, checked before any model call."""
    return label_rule(labels) or text_rule(f"{title}\n{body}")
