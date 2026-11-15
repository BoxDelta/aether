"""Keep the core/log.py docstring in step with what is redacted (#35)."""

import ast
import inspect

from core import log
from core.config import Settings


def _settings_read_by_secrets_from() -> set[str]:
    """Settings fields ``secrets_from()`` reads, as their environment variable names."""
    tree = ast.parse(inspect.getsource(log.secrets_from))
    fields = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    }
    assert fields <= set(Settings.model_fields), f"not a Settings field: {fields}"
    return {name.upper() for name in fields}


def test_every_secret_source_is_documented() -> None:
    sources = _settings_read_by_secrets_from()
    assert sources, "secrets_from() reads no settings; update this test"
    doc = log.__doc__ or ""
    missing = {name for name in sources if f"``{name}``" not in doc}
    assert not missing, f"core/log.py docstring does not list: {sorted(missing)}"


def test_every_key_pattern_is_documented() -> None:
    # One documented bullet per pattern; update both together.
    documented = {"sk-ant-": "``sk-ant-...``", "earer ": "``Bearer <credential>``"}
    doc = log.__doc__ or ""
    assert len(log._KEY_PATTERNS) == len(documented), (
        "_KEY_PATTERNS changed; document the new pattern in core/log.py and here"
    )
    for fragment, wording in documented.items():
        assert any(fragment in p.pattern for p in log._KEY_PATTERNS), fragment
        assert wording in doc, f"core/log.py docstring does not mention {wording}"
