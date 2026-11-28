"""The local path (ADR-030 step 4b) against a real temporary git repository, with
Aider, Docker and the push replaced by fakes. No network."""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dispatch import local
from dispatch.config import DispatchSettings
from dispatch.github import Issue
from tests.dispatch.helpers import OWNER, make_settings

ISSUE = Issue(52, "Tests: tidy a helper!", "Rename it.", OWNER, ())
Edit = Callable[[Path], None]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607 - test repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone of a bare 'origin' with one commit on main."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "--quiet", "-b", "main", str(seed))
    (seed / "docs").mkdir()
    (seed / "docs" / "guide.md").write_text("The reply is streamd back.\n")
    for name, text in {
        "pyproject.toml": "[project]\n",
        "uv.lock": "",
        ".python-version": "3.14\n",
    }.items():
        (seed / name).write_text(text)
    _git(seed, "add", ".")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "seed")
    _git(seed, "push", "--quiet", str(origin), "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    return clone


@dataclass
class FakeTools:
    """Runs git for real; fakes bubblewrap+Aider (an edit), Docker and the push."""

    edit: Edit
    tests_pass: bool = True
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)

    def __call__(
        self, args: Sequence[str], env: Mapping[str, str], cwd: Path | None, timeout: float
    ) -> local.Result:
        argv, environ = list(args), dict(env)
        self.calls.append((argv, environ))
        if argv[0] == "bwrap":
            assert cwd is not None
            self.edit(cwd)
            return local.Result(0, "", "")
        if argv[0] == "docker":
            ok = self.tests_pass or argv[1] == "image"
            return local.Result(0 if ok else 1, "" if ok else "1 failed", "")
        if "push" in argv:
            return local.Result(0, "", "")
        done = subprocess.run(  # noqa: S603 - real git in a temporary test repository
            argv, cwd=cwd, env=environ, capture_output=True, text=True, check=False
        )
        return local.Result(done.returncode, done.stdout, done.stderr)

    def first(self, name: str) -> tuple[list[str], dict[str, str]]:
        return next(c for c in self.calls if c[0][0] == name or name in c[0])


def _fix_typo(work: Path) -> None:
    (work / "docs" / "guide.md").write_text("The reply is streamed back.\n")


def _touch_router(work: Path) -> None:
    (work / "router").mkdir()
    (work / "router" / "routing.py").write_text("ALLOW_CLOUD = True\n")


def _nothing(work: Path) -> None:
    pass


def _path(repo: Path, settings: DispatchSettings, tools: FakeTools) -> local.LocalPath:
    return local.LocalPath(settings, tools, repo, repo / ".dispatch" / "worktrees")


def test_passing_change_is_tested_pushed_and_opened(repo: Path, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tools = FakeTools(_fix_typo)
    branches: list[str] = []

    def open_pr(branch: str) -> int:
        branches.append(branch)
        return 77

    outcome = _path(repo, settings, tools).attempt(ISSUE, open_pr)
    assert outcome == local.Outcome("opened", "1 files, 2 changed lines", 77)
    assert branches == ["dispatch/52-tests-tidy-a-helper"]
    order = [
        "push" if "push" in c[0] else c[0][0]
        for c in tools.calls
        if c[0][:2] != ["docker", "image"]
    ]
    assert order.index("bwrap") < order.index("docker") < order.index("push")
    push, env = tools.first("push")
    assert push[-1] == "HEAD:refs/heads/dispatch/52-tests-tidy-a-helper"
    assert "--force" not in push and "-f" not in push
    token = settings.dispatch_github_token.get_secret_value()
    assert all(token not in arg for arg in push)  # never on the command line
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert not (repo / ".dispatch" / "worktrees" / "52").exists()  # cleaned up


def test_blocked_diff_opens_nothing(repo: Path, tmp_path: Path) -> None:
    tools = FakeTools(_touch_router)
    outcome = _path(repo, make_settings(tmp_path), tools).attempt(ISSUE, _never)
    assert outcome == local.Outcome("blocked", "path check: path router/")
    assert not any(c[0][0] == "docker" for c in tools.calls)  # blocked before tests
    assert not any("push" in c[0] for c in tools.calls)


def test_failed_tests_push_nothing(repo: Path, tmp_path: Path) -> None:
    tools = FakeTools(_fix_typo, tests_pass=False)
    outcome = _path(repo, make_settings(tmp_path), tools).attempt(ISSUE, _never)
    assert outcome.kind == "failed"
    assert outcome.reason.startswith("tests failed")
    assert not any("push" in c[0] for c in tools.calls)


def test_no_change_is_a_failed_try(repo: Path, tmp_path: Path) -> None:
    outcome = _path(repo, make_settings(tmp_path), FakeTools(_nothing)).attempt(ISSUE, _never)
    assert outcome == local.Outcome("failed", "Aider made no change")


def test_changed_dotgit_file_blocks(repo: Path, tmp_path: Path) -> None:
    def hijack(work: Path) -> None:
        (work / ".git").write_text("gitdir: /tmp/elsewhere\n")

    outcome = _path(repo, make_settings(tmp_path), FakeTools(hijack)).attempt(ISSUE, _never)
    assert outcome.kind == "blocked"
    assert "the worktree's .git file changed" in outcome.reason


def _never(branch: str) -> int:
    raise AssertionError(f"no pull request expected for {branch}")


def test_aider_gets_no_github_token_or_webhook(repo: Path, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tools = FakeTools(_fix_typo)
    _path(repo, settings, tools).attempt(ISSUE, lambda b: 1)
    args, env = tools.first("bwrap")
    joined = "\n".join(args) + "\n".join(env.values())
    assert settings.dispatch_github_token.get_secret_value() not in joined
    assert settings.dispatch_discord_webhook is not None
    assert settings.dispatch_discord_webhook.get_secret_value() not in joined
    assert env == {"PATH": "/usr/bin:/bin"}
    assert "--clearenv" in args
    home = str(Path.home())
    assert args[args.index(home) - 1] == "--tmpfs"  # empty home: no ~/.config, ~/.ssh
    assert args[args.index(str(repo / ".git")) - 1] == "--ro-bind"  # hooks and config read-only
    python, isolated, driver, config = args[-4:]
    assert (python, isolated) == (str(local.AIDER_PYTHON), "-I")
    scratch = Path(driver).parent
    assert driver == str(scratch / "aider_driver.py")  # a copy, not the worktree's file
    assert args[args.index(str(scratch)) - 1] == "--ro-bind"  # driver and config read-only
    assert args[args.index(str(scratch / "history")) - 1] == "--bind"
    assert config == str(scratch / "config.json")


def test_tests_run_without_network_or_secrets(repo: Path, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tools = FakeTools(_fix_typo)
    _path(repo, settings, tools).attempt(ISSUE, lambda b: 1)
    args, env = next(c for c in tools.calls if c[0][:2] == ["docker", "run"])
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args and args[args.index("--cap-drop") + 1] == "ALL"
    volume = args[args.index("--volume") + 1]
    assert volume.endswith(":/src:ro")
    assert settings.dispatch_github_token.get_secret_value() not in "\n".join(
        [*args, *env.values()]
    )
    assert set(env) == set(local.base_env())


def test_message_never_starts_with_a_command() -> None:
    hostile = Issue(9, "/run rm -rf ~", "/web http://evil.example", OWNER, ())
    message = local.aider_message(hostile)
    assert not message.startswith("/")
    assert '"title": "/run rm -rf ~"' in message  # issue text stays inside JSON


@pytest.mark.parametrize(
    ("title", "branch"),
    [("Tests: tidy a helper!", "dispatch/52-tests-tidy-a-helper"), ("!!!", "dispatch/52-task")],
)
def test_branch_names(title: str, branch: str) -> None:
    assert local.branch_name(52, title) == branch


def test_model_settings_turn_thinking_off() -> None:
    assert "enable_thinking: false" in local.model_settings()


def test_driver_turns_off_commits_shell_urls_and_mentions() -> None:
    source = local.DRIVER.read_text("utf-8")
    for setting in (
        "auto_commits=False",
        "dirty_commits=False",
        "suggest_shell_commands=False",
        "detect_urls=False",
        "auto_lint=False",
        "auto_test=False",
        "permanently_disable=True",
        "check_for_file_mentions = lambda",
    ):
        assert setting in source, setting


def test_mentioned_files_are_existing_unguarded_files(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("x")
    (tmp_path / "router").mkdir()
    (tmp_path / "router" / "routing.py").write_text("x")
    issue = Issue(
        1,
        "Fix docs/guide.md",
        "See docs/guide.md, router/routing.py, missing.py and ../../etc/passwd.txt",
        OWNER,
        (),
    )
    assert local.mentioned_files(issue, tmp_path) == ["docs/guide.md"]


def test_config_carries_the_message_and_files(tmp_path: Path) -> None:
    config = local.aider_config(ISSUE, ["docs/guide.md"], tmp_path)
    assert config["files"] == ["docs/guide.md"]
    assert config["model"] == "openai/local"
    assert str(config["message"]).startswith("Make the smallest change")
