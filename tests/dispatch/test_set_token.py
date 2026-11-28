"""scripts/dispatch_set_token.py (ADR-041): the token comes from stdin only, must be
the machine user's with exactly public_repo, replaces the old value, never printed."""

import io
import secrets
from pathlib import Path

import pytest

from scripts import dispatch_set_token as tool


def _token() -> str:
    return "ghp_" + secrets.token_hex(18)


class _Stdin(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "dispatch.env"
    path.write_text("DISPATCH_GITHUB_TOKEN=old-owner-token\nDISPATCH_OWNER=BoxDelta\n")
    path.chmod(0o600)
    monkeypatch.setenv("DISPATCH_ENV_FILE", str(path))
    return path


def _run(monkeypatch: pytest.MonkeyPatch, token: str, login: str, scopes: str) -> None:
    monkeypatch.setattr(tool, "check", lambda _t: (login, scopes, "2027-02-25 12:00:00 UTC"))
    monkeypatch.setattr("sys.stdin", _Stdin(token + "\n"))
    tool.main()


def test_replaces_the_token_and_never_prints_it(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _token()
    _run(monkeypatch, token, tool.BOT_LOGIN, "public_repo")
    text = env.read_text()
    assert f"DISPATCH_GITHUB_TOKEN={token}\n" in text
    assert "old-owner-token" not in text and "DISPATCH_OWNER=BoxDelta" in text
    assert text.count("DISPATCH_GITHUB_TOKEN=") == 1
    assert env.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr()
    assert token not in out.out + out.err
    assert "boxdelta-dispatch, public_repo only" in out.out


@pytest.mark.parametrize(
    ("login", "scopes", "message"),
    [
        ("BoxDelta", "public_repo", "not boxdelta-dispatch"),
        (tool.BOT_LOGIN, "repo", "exactly public_repo"),
        (tool.BOT_LOGIN, "public_repo, workflow", "exactly public_repo"),
    ],
)
def test_wrong_token_changes_nothing(
    env: Path, monkeypatch: pytest.MonkeyPatch, login: str, scopes: str, message: str
) -> None:
    before = env.read_text()
    with pytest.raises(SystemExit, match=message):
        _run(monkeypatch, _token(), login, scopes)
    assert env.read_text() == before


def test_only_classic_tokens(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="classic"):
        _run(monkeypatch, "github_pat_" + secrets.token_hex(10), tool.BOT_LOGIN, "public_repo")
    assert "old-owner-token" in env.read_text()
