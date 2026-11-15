"""ProviderName and the safe-ID pattern are defined once (#33).

The events table's CHECK constraints keep frozen copies in the migration; this
test fails if a definition and its frozen copy drift apart.
"""

import ast
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from core import request_context
from router import contract

ROOT = Path(__file__).resolve().parent.parent
DEFINITIONS = {
    "ProviderName": "router/contract.py",
    "SafeId": "core/request_context.py",
}
PATTERNS = {
    contract.PROVIDER_NAME_PATTERN: "router/contract.py",
    request_context.SAFE_ID_PATTERN: "core/request_context.py",
}


def _sources() -> list[Path]:
    return sorted(p for d in ("core", "router") for p in (ROOT / d).rglob("*.py"))


def test_one_definition_each() -> None:
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text())
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for name, home in DEFINITIONS.items():
            if rel != home:
                assert name not in assigned, f"{rel} redefines {name}; import it from {home}"
        for pattern, home in PATTERNS.items():
            if rel != home:
                assert pattern not in path.read_text(), f"{rel} repeats the {home} pattern"


def test_migration_keeps_frozen_copies() -> None:
    text = (ROOT / "migrations" / "versions" / "0002_events.py").read_text()
    safe_id = f"'^{request_context.SAFE_ID_PATTERN}$'"
    provider = f"'^{contract.PROVIDER_NAME_PATTERN}$'"
    for column in ("request_id", "session_id"):
        assert f"{column} ~ {safe_id}" in text, column
    for column in ("override", "provider"):
        assert f"{column} ~ {provider}" in text, column


@pytest.mark.parametrize(
    "value",
    ["abc-123", "a.b_c", "x" * 128, "x" * 129, "", "bad id", "new\nline", "é", "a/b"],
)
def test_middleware_and_event_model_agree_on_ids(value: str) -> None:
    adapter = TypeAdapter(request_context.SafeId)
    try:
        adapter.validate_python(value)
        model_ok = True
    except ValidationError:
        model_ok = False
    assert request_context.is_safe_id(value) is model_ok
