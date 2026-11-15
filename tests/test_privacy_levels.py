"""One source of truth for privacy levels (#58)."""

import ast
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from core.identity import UserContextPolicy
from router.contract import (
    NON_SECRET_LEVELS,
    PRIVACY_ORDER,
    SECRET,
    PrivacyLevel,
    ProviderInfo,
    privacy_rank,
)

REPO = Path(__file__).resolve().parent.parent
NAMES = {"public", "personal", "sensitive", "secret"}


def test_order_comes_from_the_literal() -> None:
    assert get_args(PrivacyLevel) == PRIVACY_ORDER
    assert PRIVACY_ORDER == ("public", "personal", "sensitive", "secret")
    assert [privacy_rank(level) for level in PRIVACY_ORDER] == list(range(len(PRIVACY_ORDER)))


def test_non_secret_set_is_a_strict_subset_without_secret() -> None:
    assert SECRET == "secret"  # noqa: S105 - a privacy level name, not a password
    assert PRIVACY_ORDER[-1] == SECRET  # the highest level
    assert set(NON_SECRET_LEVELS) < set(PRIVACY_ORDER)
    assert SECRET not in NON_SECRET_LEVELS
    assert set(NON_SECRET_LEVELS) | {SECRET} == set(PRIVACY_ORDER)


@pytest.mark.parametrize("level", NON_SECRET_LEVELS)
def test_non_secret_levels_accepted_where_secret_is_not(level: PrivacyLevel) -> None:
    assert UserContextPolicy(include_by_default=(level,)).include_by_default == (level,)
    info = ProviderInfo(name="p", kind="local", default_model="m", max_privacy=level)
    assert info.max_privacy == level


@pytest.mark.parametrize("bad", ["secret", "Secret", "unknown", ""])
def test_secret_and_unknown_refused_where_only_non_secret_allowed(bad: str) -> None:
    with pytest.raises(ValidationError):
        UserContextPolicy(include_by_default=("public", bad))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ProviderInfo(name="p", kind="local", default_model="m", max_privacy=bad)  # type: ignore[arg-type]


def _string_tuples_of_level_names(tree: ast.AST) -> list[tuple[int, list[str]]]:
    # Literal["a", "b"] parses as a Subscript whose slice is a Tuple, so Tuple/List/Set
    # covers literals, tuples, lists and sets.
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            names = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if len(set(names) & NAMES) >= 2:
                found.append((node.lineno, names))
    return found


def test_level_names_listed_only_in_router_contract() -> None:
    # Any tuple/list/set/Literal of two or more level names outside router/contract.py
    # is a second list that could drift. Tests and the frozen migration are exempt
    # (the migration is checked against PRIVACY_ORDER below).
    offenders = []
    for folder in ("core", "router", "identity"):
        for path in sorted((REPO / folder).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel == "router/contract.py":
                continue
            for line, names in _string_tuples_of_level_names(ast.parse(path.read_text())):
                offenders.append(f"{rel}:{line} {names}")
    assert offenders == []

    contract = _string_tuples_of_level_names(ast.parse((REPO / "router/contract.py").read_text()))
    assert len(contract) == 1, contract  # exactly the PrivacyLevel literal


def test_detector_catches_a_second_list() -> None:
    sample = 'X = Literal["public", "personal"]\nY = ("sensitive", "secret")\nZ = ["ok", "public"]'
    assert sorted(line for line, _ in _string_tuples_of_level_names(ast.parse(sample))) == [1, 2]


def test_events_migration_check_matches_privacy_order() -> None:
    # Migrations are frozen snapshots and don't import app code; this catches drift.
    source = (REPO / "migrations/versions/0002_events.py").read_text()
    m = re.search(r'_in\(\s*"privacy",\s*\(([^)]*)\)', source)
    assert m, "events_privacy CHECK not found"
    assert tuple(re.findall(r'"(\w+)"', m.group(1))) == PRIVACY_ORDER
