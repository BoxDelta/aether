"""Every GitHub Action is pinned to a full commit SHA with a version comment (#61).

A tag like ``@v7`` can be moved to new code; a 40-character commit SHA can't.
Dependabot (``github-actions`` ecosystem) updates the SHA and the comment together.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"^\s+#\s+v\d+\.\d+\.\d+\s*$")


def uses_lines() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted((REPO / ".github" / "workflows").glob("*.y*ml")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if m := USES.match(line):
                found.append((f"{path.name}:{number}", m["ref"], m["rest"]))
    return found


def test_workflows_use_actions() -> None:
    assert uses_lines(), "no `uses:` found: the scan is broken"


def test_every_action_pinned_by_sha_with_version_comment() -> None:
    bad = [
        f"{where} {ref}{rest}"
        for where, ref, rest in uses_lines()
        if not (PINNED.fullmatch(ref) and VERSION_COMMENT.match(rest))
    ]
    assert bad == []


def test_detector_rejects_tags_and_short_shas() -> None:
    for ref in ("actions/checkout@v7", "actions/checkout@3d3c42e", "actions/checkout@main"):
        assert not PINNED.fullmatch(ref)
    assert PINNED.fullmatch("actions/checkout@" + "a" * 40)
    assert not VERSION_COMMENT.match(" # v7")
    assert VERSION_COMMENT.match(" # v7.0.1")
