"""Static guards: no GitHub write or merge call in dispatch/, and Aether never imports it.

ADR-030 §Credentials: the token could merge a passing PR, so the code must have
no merge call. In step 4a the dispatcher makes no GitHub write at all; its only
non-GET calls are the POST to LiteLLM in classify.py and the Discord webhook POST
in notify.py (#135).
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "dispatch"
MODULES = sorted(DISPATCH.glob("*.py"))
WRITE_ATTRS = frozenset({"post", "patch", "put", "delete", "request", "send", "stream"})
WRITE_WORDS = re.compile(r"\b(POST|PATCH|PUT|DELETE)\b")
ALLOWED_CALLS = [
    ("classify.py", "post", "/chat/completions"),
    ("brief.py", "post", "/chat/completions"),
    ("notify.py", "post", None),  # the webhook URL from settings, checked below
]


def _calls(path: Path) -> list[tuple[str, str, str | None]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITE_ATTRS
        ):
            first = node.args[0] if node.args else None
            url = first.value if isinstance(first, ast.Constant) else None
            found.append((path.name, node.func.attr, url if isinstance(url, str) else None))
    return found


def _strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"))
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_modules_found() -> None:
    assert {p.name for p in MODULES} >= {"github.py", "classify.py", "rules.py", "__main__.py"}


def test_only_write_calls_are_litellm_and_webhook() -> None:
    calls = [call for path in MODULES for call in _calls(path)]
    assert sorted(calls, key=str) == sorted(ALLOWED_CALLS, key=str)


def test_webhook_post_uses_the_validated_webhook_only() -> None:
    tree = ast.parse((DISPATCH / "notify.py").read_text("utf-8"))
    posts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
    ]
    assert len(posts) == 1
    assert ast.unparse(posts[0].args[0]) == "webhook.get_secret_value()"


def test_notify_has_no_github_client_or_token() -> None:
    source = (DISPATCH / "notify.py").read_text("utf-8")
    assert "dispatch.github" not in source
    assert "dispatch_github_token" not in source
    assert "api.github.com" not in source


def test_no_write_method_names_in_strings() -> None:
    for path in MODULES:
        for text in _strings(path):
            assert not WRITE_WORDS.search(text), (path.name, text)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_merge_endpoint(path: Path) -> None:
    # The merge endpoint is PUT /repos/{o}/{r}/pulls/{n}/merge; "safemerge.fish" is a path rule.
    for text in _strings(path):
        assert not re.search(r"(?<![a-z])merge|pulls", text.lower()), (path.name, text)


def test_github_client_never_used_for_litellm() -> None:
    tree = ast.parse((DISPATCH / "classify.py").read_text("utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "dispatch.github"
        for alias in node.names
    }
    assert imported <= {"Issue"}


def test_dispatch_imports_only_router_hardware_from_aether() -> None:
    for path in MODULES:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top in {"core", "memory", "migrations"}:
                    pytest.fail(f"{path.name} imports {node.module}")
                if top == "router":
                    assert node.module == "router" and {a.name for a in node.names} == {"hardware"}


@pytest.mark.parametrize("package", ["core", "router", "memory"])
def test_aether_never_imports_dispatch(package: str) -> None:
    for path in (ROOT / package).rglob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+dispatch\b", path.read_text("utf-8"), re.M), path
