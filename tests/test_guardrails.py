"""Keep docs/05 §10.3 in step with the Claude Code ask rules (#84)."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / ".claude" / "settings.json"
DOCS_05 = ROOT / "docs" / "05_Security_and_Permissions.md"


def _ask_edit_paths() -> set[str]:
    ask = json.loads(SETTINGS.read_text())["permissions"]["ask"]
    paths = set()
    for rule in ask:
        if not rule.startswith("Edit("):
            continue
        match = re.fullmatch(r"Edit\(/([^/].*)\)", rule)
        assert match, f"unexpected Edit ask rule form, update this test: {rule}"
        paths.add(match.group(1))
    return paths


def _documented_ask_paths() -> set[str]:
    """Backticked file paths in the *ask* bullet of docs/05 §10.3."""
    text = DOCS_05.read_text()
    section = text[text.index("### 10.3") :]
    bullet = section[section.index("*ask*:") : section.index("Deny wins")]
    tokens = re.findall(r"`([^`]+)`", bullet)
    return {t for t in tokens if " " not in t and ("." in t or "/" in t)}


def test_settings_local_json_edits_need_approval() -> None:
    assert ".claude/settings.local.json" in _ask_edit_paths()


def test_docs_05_ask_list_matches_settings() -> None:
    settings = _ask_edit_paths()
    documented = _documented_ask_paths()
    assert settings, "no Edit(...) ask rules found in .claude/settings.json"
    assert not settings - documented, (
        f"in settings.json but not in docs/05 §10.3: {sorted(settings - documented)}"
    )
    assert not documented - settings, (
        f"in docs/05 §10.3 but not in settings.json: {sorted(documented - settings)}"
    )
