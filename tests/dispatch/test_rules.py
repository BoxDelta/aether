"""Every always-Claude rule (ADR-030, merged list), by label, by text and by changed path."""

import re
from pathlib import Path

import pytest

from dispatch import rules

ROOT = Path(__file__).resolve().parents[2]

CORE_PATHS = [f"core/{name}" for name in rules.CORE_FILES]
ALL_PATHS = [*rules.ALWAYS_CLAUDE_PATHS, *CORE_PATHS]


def test_list_matches_adr_030() -> None:
    assert set(rules.ALWAYS_CLAUDE_PATHS) == {
        ".github/", ".claude/", "CLAUDE.md", ".pre-commit-config.yaml",
        "scripts/safemerge.fish", ".env.example", "pyproject.toml", "uv.lock",
        "router/", "memory/", "migrations/", "docs/05_", "docs/adr/", "dispatch/",
        "tools/", "identity/", ".env", ".gitignore", "CONTRIBUTING.md",
        "docker-compose.yml", "compose.vault.yaml", "scripts/systemd/",
        "scripts/restore_check.sql", "scripts/aether-vault.fish",
        "scripts/identity_continuity.py", "tests/test_workflow_pins.py",
        "docs/runbooks/db-app-role.md", "docs/runbooks/db-backup-restore.md",
        "docs/runbooks/encrypted-volume.md", "SECURITY.md", "LICENSE", "NOTICE",
        ".dispatch/", "tests/dispatch/", "tests/test_cloud_tripwire.py",
        "tests/test_env_isolation.py", "tests/test_error_bodies.py",
        "tests/test_guardrails.py", "tests/test_log_redaction_docs.py",
        "tests/test_ollama_host_trust.py", "tests/test_privacy_levels.py",
        "tests/test_routing.py", "tests/test_safemerge.py",
        "tests/test_untrusted_ollama.py",
    }  # fmt: skip
    assert set(rules.CORE_FILES) == {
        "auth.py", "config.py", "chat.py", "memory_api.py",
        "identity.py", "db.py", "db_admin.py", "backup.py",
        "app.py", "errors.py", "events.py", "log.py", "ready.py", "request_context.py",
    }  # fmt: skip
    assert set(rules.ALWAYS_CLAUDE_LABELS) == {"model:opus", "security", "dispatch:claude-only"}


def test_list_is_quoted_in_adr_030() -> None:
    adr = (ROOT / "docs/adr/0030-dispatcher-prototype.md").read_text("utf-8")
    for path in rules.ALWAYS_CLAUDE_PATHS:
        shown = "docs/05_*" if path == "docs/05_" else path
        assert f"`{shown}`" in adr, path
    for name in rules.CORE_FILES:
        assert f"`{name}`" in adr, name
    for label in rules.ALWAYS_CLAUDE_LABELS:
        assert f"`{label}`" in adr, label


def _docs05_paths() -> list[str]:
    text = (ROOT / "docs/05_Security_and_Permissions.md").read_text("utf-8")
    found = re.findall(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*)`", text)
    return sorted({p for p in found if "/" in p or "." in p} & _tracked() | _dirs(found))


def _tracked() -> set[str]:
    return {
        str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git/" not in str(p)
    }


def _dirs(found: list[str]) -> set[str]:
    return {p for p in found if p.endswith("/") and (ROOT / p).is_dir()}


def test_every_file_docs05_names_is_on_the_list() -> None:
    named = _docs05_paths()
    assert "core/log.py" in named  # the scan finds files
    missing = [p for p in named if rules.path_rule(p) is None]
    assert missing == [], "add these to dispatch/rules.py and ADR-030"


@pytest.mark.parametrize("label", ["model:opus", "security", "Security", "dispatch:claude-only"])
def test_labels(label: str) -> None:
    assert rules.issue_rule("Tidy tests", "", [label]) == f"label {label.lower()}"


def test_dependency_label() -> None:
    assert (
        rules.issue_rule("Bump x", "", ["dependencies"]) == "adds a dependency (label dependencies)"
    )


def _example(path: str) -> str:
    if path == "docs/05_":
        return "docs/05_Security_and_Permissions.md"
    return path + "file.py" if path.endswith("/") else path


@pytest.mark.parametrize("path", ALL_PATHS)
def test_each_path_in_body(path: str) -> None:
    assert rules.issue_rule("Small change", f"Edit {_example(path)} please.", []) is not None


@pytest.mark.parametrize("path", ALL_PATHS)
def test_each_path_in_title(path: str) -> None:
    assert rules.issue_rule(f"Change {_example(path)}", "", []) is not None


@pytest.mark.parametrize("path", ALL_PATHS)
def test_each_path_in_diff(path: str) -> None:
    assert rules.path_rule(_example(path)) is not None


@pytest.mark.parametrize(
    "text",
    [
        "see docs/05 §10.1",
        "the .github workflows",
        "bare auth.py needs a fix",
        "GET /memory/export",
        "Router: guard on_decision in router/routing.py",
    ],
)
def test_broad_text_forms(text: str) -> None:
    assert rules.text_rule(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "This adds a new dependency on rich.",
        "Add the requests package",
        "run uv add foo",
        "pip install bar",
        "This needs an ADR first.",
        "Write a new ADR for it",
    ],
)
def test_dependency_and_adr_keywords(text: str) -> None:
    assert rules.text_rule(text) in {"adds a dependency", "needs an ADR"}


@pytest.mark.parametrize(
    "text",
    [
        "Fix a typo in docs/CODE_TOUR.md",
        "tests/test_db.py is slow",
        "see https://api.github.com/repos",
        "tests/test_chat_output_cap.py: add a case",
        "as ADR-024 says",
    ],
)
def test_ordinary_text_does_not_match(text: str) -> None:
    assert rules.text_rule(text) is None


@pytest.mark.parametrize(
    "path",
    ["docs/CODE_TOUR.md", "tests/test_db.py", "README.md", "web/src/Sidebar.tsx"],
)
def test_ordinary_paths_do_not_match(path: str) -> None:
    assert rules.path_rule(path) is None


@pytest.mark.parametrize(
    "text",
    [
        "see docs/adr for it",
        "edit router\u200b/routing.py",
        "edit " + "".join(chr(ord(c) + 0xFEE0) for c in "router") + "/routing.py",
    ],
)
def test_normalized_text_still_matches(text: str) -> None:
    assert rules.text_rule(text) is not None


@pytest.mark.parametrize("text", ["the Memory API pages", "Router: guard a callback"])
def test_bare_words_are_not_paths(text: str) -> None:
    assert rules.text_rule(text) is None


@pytest.mark.parametrize(
    "path",
    [
        "core/log.py",
        "core/auth_tokens.py",
        "web/src/auth/Login.tsx",
        "web/src/LoginScreen.tsx",
        "tests/test_auth.py",
        "core/csrf.py",
        "tools/web_search.py",
        ".env",
        "sub/.env.local",
        "scripts/systemd/aether-backup.service",
        ".dispatch/brief-template.md",
        "tests/dispatch/test_boundaries.py",
        "core/sessions.py",
        "core/service_tokens.py",
        "core/approvals.py",
        "web/src/permissions/Levels.tsx",
        "core/redact.py",
    ],
)
def test_new_paths_block(path: str) -> None:
    assert rules.path_rule(path) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Found in the security review of #1 and #12.",
        "From the security-reviewer report on PR #40",
        "The fixture creates a temporary login role aether_app_test_x",
        "CREATE ROLE foo; GRANT SELECT ON t TO foo",
        "revoke the privileges of the app role",
        "rotate the database credentials",
        "store the API key in the env file",
        "the password is hashed",
    ],
)
def test_issue_61_rule(text: str) -> None:
    assert rules.text_rule(text) is not None


def test_issue_61_text_goes_to_claude() -> None:
    body = (
        "Found in the security review of #1 and #12.\n\nThe live test fixture `live_app` "
        "(tests/conftest.py) creates a temporary login role `aether_app_test_<random>`."
    )
    assert rules.issue_rule("Tests: leftover role if a live test is killed", body, []) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Tests: tests/test_chat.py has a duplicated helper",
        "docs: the CODE_TOUR auth section has a typo",
        "Rename the chat role field in the message list",
    ],
)
def test_issue_61_rule_leaves_ordinary_text(text: str) -> None:
    assert rules.text_rule(text) is None
