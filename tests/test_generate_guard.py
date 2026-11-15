"""Guard for #18: providers are called only through call_with_identity.

Scans every .py file in the repo (except tests/ and tool/venv directories) with
``ast`` and reports each violation as file:line. Each rule allows exactly one file:

- ``.generate`` attribute access: only router/identity_call.py
  (``call_with_identity`` applies the identity and logs the call);
- ``._generate`` attribute access: only router/contract.py
  (``Provider.generate`` wraps it with the privacy check, timeout and error
  scrubbing; calling it directly skips all three);
- importing ``anthropic``: only router/claude.py;
- importing ``ollama``: only router/ollama.py.

Attribute access means any use, not only calls: an alias (``g = p.generate``) or
an assignment is flagged too. Imports cover ``import x``, ``import x.y as z``,
``from x[.y] import ...`` and ``importlib.import_module("x")`` /
``__import__("x")`` with a literal name. Fully dynamic access (names built at
runtime, ``getattr``) is out of reach of a static scan.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    "tests",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}

# Rule -> the only file allowed to break it.
ATTRIBUTE_RULES = {
    "generate": Path("router/identity_call.py"),
    "_generate": Path("router/contract.py"),
}
IMPORT_RULES = {
    "anthropic": Path("router/claude.py"),
    "ollama": Path("router/ollama.py"),
}
DYNAMIC_IMPORTERS = {"import_module", "__import__"}


def python_files() -> list[Path]:
    files = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if not any(part in SKIP_DIRS for part in rel.parts[:-1]):
            files.append(rel)
    return sorted(files)


def _top(module: str) -> str:
    return module.split(".", 1)[0]


def _imported_sdk(node: ast.AST) -> list[str]:
    """Top-level SDK names (anthropic/ollama) this node imports, if any."""
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [_top(alias.name) for alias in node.names]
    elif isinstance(node, ast.ImportFrom):
        # Relative imports (from . import ollama) are this repo's modules.
        if node.level == 0 and node.module:
            names = [_top(node.module)]
    elif isinstance(node, ast.Call) and node.args:
        func = node.func
        fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        first = node.args[0]
        if (
            fname in DYNAMIC_IMPORTERS
            and isinstance(first, ast.Constant)
            and isinstance(first.value, str)
        ):
            names = [_top(first.value)]
    return [n for n in names if n in IMPORT_RULES]


def find_uses(source: str, filename: str = "<sample>") -> list[tuple[int, str]]:
    """Every (line, rule) in ``source``. Rules: '.generate', '._generate',
    'import anthropic', 'import ollama'."""
    uses: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Attribute) and node.attr in ATTRIBUTE_RULES:
            uses.append((node.lineno, f".{node.attr}"))
        elif isinstance(node, ast.Import | ast.ImportFrom | ast.Call):
            uses.extend((node.lineno, f"import {sdk}") for sdk in _imported_sdk(node))
    return sorted(uses)


def _allowed_file(rule: str) -> Path:
    if rule.startswith("import "):
        return IMPORT_RULES[rule.removeprefix("import ")]
    return ATTRIBUTE_RULES[rule.removeprefix(".")]


def format_violations(rel: Path, source: str) -> list[str]:
    return [
        f"{rel}:{line} {rule} (only allowed in {_allowed_file(rule)})"
        for line, rule in find_uses(source, str(rel))
        if rel != _allowed_file(rule)
    ]


def violations(rel: Path) -> list[str]:
    return format_violations(rel, (REPO / rel).read_text(encoding="utf-8"))


# --- the scan --------------------------------------------------------------------


def test_scan_covers_the_router() -> None:
    files = python_files()
    for path in [*ATTRIBUTE_RULES.values(), *IMPORT_RULES.values()]:
        assert path in files
    assert Path("router/routing.py") in files
    assert Path("core/app.py") in files
    assert not any(f.parts[0] == "tests" for f in files)


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        (ATTRIBUTE_RULES["generate"], ".generate"),
        (ATTRIBUTE_RULES["_generate"], "._generate"),
        (IMPORT_RULES["anthropic"], "import anthropic"),
        (IMPORT_RULES["ollama"], "import ollama"),
    ],
)
def test_scan_finds_each_allowed_use(path: Path, rule: str) -> None:
    # Proves each rule can fire on real code, so the guard can't pass by finding nothing.
    found = [r for _, r in find_uses((REPO / path).read_text(encoding="utf-8"), str(path))]
    assert rule in found, f"scan found no {rule} in {path}"


def test_no_provider_bypass() -> None:
    offenders = [v for f in python_files() for v in violations(f)]
    assert offenders == [], (
        "Call providers only through router.identity_call.call_with_identity, and keep "
        "vendor SDKs inside their adapter (#18). Found:\n  " + "\n  ".join(offenders)
    )


# --- the detector ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # .generate
        ("await p.generate(r)", ".generate"),
        ("g = p.generate", ".generate"),
        # ._generate: direct call skips privacy check, timeout and error scrubbing
        ("await p._generate(r)", "._generate"),
        ("g = p._generate", "._generate"),
        ("p._generate = fake", "._generate"),
        ("await super()._generate(r)", "._generate"),
        # anthropic
        ("import anthropic", "import anthropic"),
        ("import anthropic as a", "import anthropic"),
        ("import anthropic.types.beta", "import anthropic"),
        ("import json, anthropic", "import anthropic"),
        ("from anthropic import AsyncAnthropic", "import anthropic"),
        ("from anthropic.types.beta import BetaMessage", "import anthropic"),
        ("importlib.import_module('anthropic')", "import anthropic"),
        ("import_module('anthropic.types')", "import anthropic"),
        # ollama
        ("import ollama", "import ollama"),
        ("import ollama as o", "import ollama"),
        ("from ollama import AsyncClient", "import ollama"),
        ("from ollama._types import ChatResponse", "import ollama"),
        ("__import__('ollama')", "import ollama"),
    ],
)
def test_detector_catches(source: str, expected: str) -> None:
    assert find_uses(source) == [(1, expected)]


@pytest.mark.parametrize(
    "source",
    [
        "async def _generate(self, r): ...",  # defining the hook is how adapters work
        "async def generate(self, r): ...",
        "x = generate_report()",
        "p.regenerate()",
        "import anthropic_tools",  # different package, same prefix
        "import ollamax",
        "from router import ollama",  # our adapter module, not the SDK
        "from router.claude import ClaudeProvider",
        "from . import ollama",
        "importlib.import_module('pynvml')",
        "import_module(name)",  # not a literal: out of reach, documented
        "text = 'p._generate(r)'",
    ],
)
def test_detector_ignores(source: str) -> None:
    assert find_uses(source) == []


def test_detector_reports_each_line() -> None:
    source = "import ollama\n\nasync def f(p, r):\n    await p._generate(r)\n"
    assert find_uses(source) == [(1, "import ollama"), (4, "._generate")]


def test_violation_report_names_file_line_and_allowed_file() -> None:
    source = "from ollama import AsyncClient\nawait p._generate(r)\nawait p.generate(r)\n"
    assert format_violations(Path("router/_bypass.py"), source) == [
        "router/_bypass.py:1 import ollama (only allowed in router/ollama.py)",
        "router/_bypass.py:2 ._generate (only allowed in router/contract.py)",
        "router/_bypass.py:3 .generate (only allowed in router/identity_call.py)",
    ]


def test_allowed_file_may_only_use_its_own_rule() -> None:
    # The adapter that may import ollama still may not call ._generate or import anthropic.
    source = "import ollama\nimport anthropic\nawait self._generate(r)\n"
    assert format_violations(Path("router/ollama.py"), source) == [
        "router/ollama.py:2 import anthropic (only allowed in router/claude.py)",
        "router/ollama.py:3 ._generate (only allowed in router/contract.py)",
    ]
