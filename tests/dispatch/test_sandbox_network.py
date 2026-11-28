"""Aider's sandbox has no network (#202): Postgres, n8n, Plane and every other host
port are unreachable from inside; the model socket is the only way out.

Runs the real bubblewrap sandbox from ``local.sandbox_args``. Skipped where
bubblewrap can't create namespaces (for example CI runners without it).
"""

import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from dispatch import bridge, local

# Aether's Postgres, n8n, Plane's proxy (HTTP and HTTPS), Langfuse, LiteLLM, llama-swap.
HOST_PORTS = (5432, 5678, 8088, 443, 3000, 4000, 9292)

PROBE = """
import socket, sys
blocked = []
for port in [int(p) for p in sys.argv[2].split(",")]:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        print("REACHED", port)
    except OSError:
        blocked.append(port)
    finally:
        s.close()
try:
    s = socket.create_connection(("1.1.1.1", 443), timeout=2)
    print("REACHED internet")
except OSError:
    pass
u = socket.socket(socket.AF_UNIX)
u.connect(sys.argv[1])
u.sendall(b"ping")
u.shutdown(socket.SHUT_WR)
print("BLOCKED", ",".join(map(str, blocked)))
print("BRIDGE", u.recv(100).decode())
"""


SANDBOX_PYTHON = Path("/usr/bin/python3")  # the sandbox sees only /usr and /etc


def _bwrap_works() -> bool:
    if shutil.which("bwrap") is None or not SANDBOX_PYTHON.exists():
        return False
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_sandbox = pytest.mark.skipif(
    not _bwrap_works(), reason="bubblewrap or /usr/bin/python3 not usable here"
)


@pytest.fixture
def fake_litellm() -> Iterator[int]:
    """A host loopback listener that answers 'pong' (stands in for LiteLLM)."""
    server = socket.create_server(("127.0.0.1", 0))
    port = int(server.getsockname()[1])

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=answer, args=(conn,), daemon=True).start()

    def answer(conn: socket.socket) -> None:  # one thread each: no connection waits
        with conn:
            conn.recv(100)
            conn.sendall(b"pong")

    threading.Thread(target=serve, daemon=True).start()
    yield port
    server.close()


@needs_sandbox
def test_no_host_port_is_reachable_and_the_bridge_works(tmp_path: Path, fake_litellm: int) -> None:
    root, work, scratch, gitdir = (tmp_path / n for n in ("root", "work", "scratch", "gitdir"))
    for folder in (root / ".git", work, scratch / "history", gitdir):
        folder.mkdir(parents=True)
    (scratch / "probe.py").write_text(PROBE)
    sock = scratch / "sock" / "llm.sock"
    ports = ",".join(str(p) for p in (*HOST_PORTS, fake_litellm))
    with bridge.unix_bridge(sock, f"http://127.0.0.1:{fake_litellm}"):
        done = subprocess.run(  # noqa: S603 - the dispatcher's own sandbox argv
            [
                *local.sandbox_args(root, work, scratch, gitdir),
                "/usr/bin/python3",
                "-I",
                str(scratch / "probe.py"),
                str(sock),
                ports,
            ],
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert done.returncode == 0, done.stderr
    assert "REACHED" not in done.stdout, done.stdout
    assert f"BLOCKED {ports}" in done.stdout
    assert "BRIDGE pong" in done.stdout


def test_bridge_only_forwards_to_http_urls() -> None:
    for url in (
        "https://127.0.0.1:4000",
        "http://example.com:4000",
        "http://10.0.0.5:4000",
        "http://localhost.evil:4000",
    ):
        with pytest.raises(ValueError, match="loopback"):
            bridge.target(url)
    assert bridge.target("http://127.0.0.1:4000") == ("127.0.0.1", 4000)
    assert bridge.target("http://localhost:4000") == ("127.0.0.1", 4000)


def test_model_socket_is_private(tmp_path: Path, fake_litellm: int) -> None:
    sock = tmp_path / "sock" / "llm.sock"
    with bridge.unix_bridge(sock, f"http://127.0.0.1:{fake_litellm}"):
        assert sock.stat().st_mode & 0o077 == 0
        assert (sock.parent.stat().st_mode & 0o777) == 0o700
    assert not sock.exists()


def test_bridge_caps_connections_and_closes_them_on_exit(
    tmp_path: Path, fake_litellm: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "MAX_CONNECTIONS", 2)
    sock = tmp_path / "sock" / "llm.sock"
    held: list[socket.socket] = []
    with bridge.unix_bridge(sock, f"http://127.0.0.1:{fake_litellm}"):
        for _ in range(3):
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            client.connect(str(sock))
            held.append(client)
        assert held[2].recv(10) == b""  # the third is closed at once
        held[0].sendall(b"ping")
        assert held[0].recv(10) == b"pong"
    assert held[1].recv(10) == b""  # still open at exit: closed by the bridge
    for client in held:
        client.close()
