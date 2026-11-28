"""The fixed path check on local diffs (ADR-030 step 4b): one passing diff, one
blocking diff, and every other way a diff is blocked."""

import pytest

from dispatch import diffcheck

PASSING_DIFF = """\
diff --git a/docs/CODE_TOUR.md b/docs/CODE_TOUR.md
index 1111111..2222222 100644
--- a/docs/CODE_TOUR.md
+++ b/docs/CODE_TOUR.md
@@ -10,7 +10,7 @@ The chat flow
 Requests enter through the API.
-The reply is streamd back.
+The reply is streamed back.
 More text.
diff --git a/tests/test_chat_helpers.py b/tests/test_chat_helpers.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_chat_helpers.py
@@ -0,0 +1,3 @@
+def test_trim() -> None:
+    assert " a ".strip() == "a"
+
"""

BLOCKING_DIFF = """\
diff --git a/router/routing.py b/router/routing.py
index 1111111..2222222 100644
--- a/router/routing.py
+++ b/router/routing.py
@@ -1,3 +1,3 @@
-ALLOW_CLOUD = False
+ALLOW_CLOUD = True
"""


def _one_file(path: str, added: str = "x = 1", header: str = "index 1..2 100644") -> str:
    return (
        f"diff --git a/{path} b/{path}\n{header}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n-old\n+{added}\n"
    )


def test_passing_diff() -> None:
    result = diffcheck.check(PASSING_DIFF)
    assert result.ok, result.reason
    assert result.files == ("docs/CODE_TOUR.md", "tests/test_chat_helpers.py")
    assert result.changed_lines == 5


def test_blocking_diff() -> None:
    result = diffcheck.check(BLOCKING_DIFF)
    assert not result.ok
    assert result.reason == "path router/"


@pytest.mark.parametrize(
    "path",
    [
        "core/auth.py",
        "web/src/auth/Login.tsx",
        "tools/shell.py",
        ".github/workflows/sentinel.yml",
        "memory/store.py",
        "docs/05_Security_and_Permissions.md",
        "core/log.py",
        "migrations/versions/0009_x.py",
        ".claude/commands/work-on.md",
        "dispatch/rules.py",
        "tests/dispatch/test_boundaries.py",
        ".dispatch/brief-template.md",
        "web/package.json",
        "requirements-dev.txt",
        ".gitattributes",
    ],
)
def test_guarded_paths_block(path: str) -> None:
    assert not diffcheck.check(_one_file(path)).ok


def test_rename_into_a_guarded_path_blocks() -> None:
    diff = (
        "diff --git a/docs/x.md b/router/x.py\nsimilarity index 100%\n"
        "rename from docs/x.md\nrename to router/x.py\n"
    )
    assert diffcheck.check(diff).reason == "path router/"


def test_name_list_catches_what_the_text_missed() -> None:
    result = diffcheck.check(_one_file("docs/x.md"), extra_paths=("core/auth.py",))
    assert result.reason == "path core/auth.py"


@pytest.mark.parametrize(
    "added",
    [
        "conn.execute('CREATE ROLE aether_app_test_x LOGIN')",
        "GRANT SELECT ON items TO reader",
        "password = 'hunter2'",
        "# found in the security review",
    ],
)
def test_sensitive_changed_lines_block(added: str) -> None:
    result = diffcheck.check(_one_file("tests/test_chat.py", added))
    assert not result.ok
    assert result.reason.startswith("changed lines:")


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("new file mode 120000", "file mode change"),  # symlink
        ("new file mode 160000", "file mode change"),  # submodule
        ("new mode 100755", "file mode change"),
    ],
)
def test_risky_modes_block(header: str, reason: str) -> None:
    assert diffcheck.check(_one_file("docs/x.md", header=header)).reason.startswith(reason)


def test_binary_blocks() -> None:
    diff = (
        "diff --git a/docs/a.png b/docs/a.png\nindex 1..2 100644\n"
        "Binary files a/docs/a.png and b/docs/a.png differ\n"
    )
    assert diffcheck.check(diff).reason == "binary file"


@pytest.mark.parametrize(
    "diff",
    [
        'diff --git "a/docs/t\\tab.md" "b/docs/t\\tab.md"\n',
        "diff --git a/../etc/x b/../etc/x\n--- a/../etc/x\n+++ b/../etc/x\n",
    ],
)
def test_odd_paths_block(diff: str) -> None:
    assert not diffcheck.check(diff).ok


def test_empty_diff_is_no_change() -> None:
    assert diffcheck.check("").reason == "no change"


def test_size_limits() -> None:
    six = "".join(_one_file(f"docs/f{i}.md") for i in range(6))
    assert diffcheck.check(six).reason == "6 files, more than 5"
    big = _one_file("docs/big.md") + "".join(f"+line {i}\n" for i in range(250))
    assert "changed lines, more than 200" in diffcheck.check(big).reason
