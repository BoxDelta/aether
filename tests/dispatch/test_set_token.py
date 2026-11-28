"""scripts/dispatch_set_token.py (ADR-041): the token comes from stdin only, must be
the machine user's with exactly public_repo, replaces the old value, never printed."""

import http.server
import io
import secrets
import threading
import urllib.error
import urllib.request
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
    _run(monkeypatch, token, tool.DEFAULT_BOT_LOGIN, "public_repo")
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
        (tool.DEFAULT_BOT_LOGIN, "repo", "exactly public_repo"),
        (tool.DEFAULT_BOT_LOGIN, "public_repo, workflow", "exactly public_repo"),
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
        _run(
            monkeypatch,
            "github_pat_" + secrets.token_hex(10),
            tool.DEFAULT_BOT_LOGIN,
            "public_repo",
        )
    assert "old-owner-token" in env.read_text()


def test_every_spelling_of_the_old_line_is_replaced(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.write_text("export DISPATCH_GITHUB_TOKEN=old1\n  DISPATCH_GITHUB_TOKEN = old2\nX=1\n")
    token = _token()
    _run(monkeypatch, token, tool.DEFAULT_BOT_LOGIN, "public_repo")
    text = env.read_text()
    assert "old1" not in text and "old2" not in text and "X=1" in text


def test_symlinked_env_file_is_refused(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "link.env"
    link.symlink_to(env)
    monkeypatch.setenv("DISPATCH_ENV_FILE", str(link))
    with pytest.raises(SystemExit, match="symlink"):
        _run(monkeypatch, _token(), tool.DEFAULT_BOT_LOGIN, "public_repo")
    assert "old-owner-token" in env.read_text()


def test_bot_login_comes_from_the_env_file(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env.write_text(env.read_text() + "DISPATCH_BOT_LOGIN=other-bot\n")
    with pytest.raises(SystemExit, match="not other-bot"):
        _run(monkeypatch, _token(), tool.DEFAULT_BOT_LOGIN, "public_repo")
    _run(monkeypatch, _token(), "other-bot", "public_repo")


def test_redirects_are_refused() -> None:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        opener = urllib.request.build_opener(tool._NoRedirects)
        with pytest.raises(urllib.error.HTTPError) as caught:
            opener.open(f"http://127.0.0.1:{server.server_port}/start", timeout=5)
        assert caught.value.code == 302
        assert hits == ["/start"]  # the redirect target was never asked
    finally:
        server.shutdown()
