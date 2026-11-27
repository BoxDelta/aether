"""Every always-Claude rule from ADR-030, by label, by text and by changed path."""

import pytest

from dispatch import rules

CORE_PATHS = [f"core/{name}" for name in rules.CORE_FILES]
ALL_PATHS = [*rules.ALWAYS_CLAUDE_PATHS, *CORE_PATHS]


def test_list_matches_adr_030() -> None:
    assert set(rules.ALWAYS_CLAUDE_PATHS) == {
        ".github/", ".claude/", "CLAUDE.md", ".pre-commit-config.yaml",
        "scripts/safemerge.fish", ".env.example", "pyproject.toml", "uv.lock",
        "router/", "memory/", "migrations/", "docs/05_", "docs/adr/", "dispatch/",
    }  # fmt: skip
    assert set(rules.CORE_FILES) == {
        "auth.py", "config.py", "chat.py", "memory_api.py",
        "identity.py", "db.py", "db_admin.py", "backup.py",
    }  # fmt: skip
    assert set(rules.ALWAYS_CLAUDE_LABELS) == {"model:opus", "security"}


@pytest.mark.parametrize("label", ["model:opus", "security", "Security"])
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
        "core/log.py docstring",
        "as ADR-024 says",
    ],
)
def test_ordinary_text_does_not_match(text: str) -> None:
    assert rules.text_rule(text) is None


@pytest.mark.parametrize(
    "path", ["docs/CODE_TOUR.md", "tests/test_db.py", "core/log.py", "README.md"]
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
