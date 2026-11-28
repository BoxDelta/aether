"""Aider's sandbox has no network (#202): Postgres, n8n, Plane and every other host
port are unreachable from inside; the model socket is the only way out.

Runs the real bubblewrap sandbox from ``local.sandbox_args``. Skipped where
bubblewrap can't create namespaces (for example CI runners without it).
"""

import http.server
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dispatch import bridge, local

# Aether's Postgres, n8n, Plane's proxy (HTTP and HTTPS), Langfuse, LiteLLM, llama-swap.
REAL_KEY = "fake-litellm-key-" + secrets.token_hex(8)
HOST_PORTS = (5432, 5678, 8088, 443, 3000, 4000, 9292)

PROBE = """
import os, socket, sys
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
print("BLOCKED", ",".join(map(str, blocked)))
print("ENV", "|".join(os.environ.values()))
def ask(request):
    u = socket.socket(socket.AF_UNIX)
    u.connect(sys.argv[1])
    u.sendall(request)
    reply = b""
    while chunk := u.recv(4096):
        reply += chunk
    return reply.split(b"\\r\\n", 1)[0].decode()
body = b'{"model":"local","messages":[]}'
print("CHAT", ask(b"POST /v1/chat/completions HTTP/1.1\\r\\nAuthorization: Bearer "
      + os.environ["OPENAI_API_KEY"].encode() + b"\\r\\nContent-Length: "
      + str(len(body)).encode() + b"\\r\\n\\r\\n" + body))
print("ADMIN", ask(b"GET /key/info HTTP/1.1\\r\\nContent-Length: 0\\r\\n\\r\\n"))
"""


SANDBOX_PYTHON = Path("/usr/bin/python3")  # the sandbox sees only /usr and /etc


def _bwrap_works() -> bool:
    """The real sandbox can start /usr/bin/python3 here. On some CI runners
    bubblewrap works but that python is a link to a path outside /usr."""
    if shutil.which("bwrap") is None or not SANDBOX_PYTHON.exists():
        return False
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dirs = [base / n for n in ("root", "work", "scratch", "gitdir")]
        for folder in (dirs[0] / ".git", dirs[1], dirs[2] / "history", dirs[3]):
            folder.mkdir(parents=True)
        probe = subprocess.run(  # noqa: S603 - the dispatcher's own sandbox argv
            [*local.sandbox_args(*dirs), str(SANDBOX_PYTHON), "-I", "-c", "pass"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    return probe.returncode == 0


needs_sandbox = pytest.mark.skipif(
    not _bwrap_works(), reason="bubblewrap or /usr/bin/python3 not usable here"
)


@dataclass
class FakeLiteLLM:
    port: int
    seen: list[tuple[str, str, str]] = field(default_factory=list)  # method, path, auth


@pytest.fixture
def fake_litellm() -> Iterator[FakeLiteLLM]:
    """A host loopback HTTP server standing in for LiteLLM; records every request."""
    fake = FakeLiteLLM(0)

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            fake.seen.append((self.command, self.path, self.headers.get("Authorization", "")))
            body = b'{"choices":[{"message":{"content":"pong"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fake.port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield fake
    server.shutdown()


def _url(fake: FakeLiteLLM) -> str:
    return f"http://127.0.0.1:{fake.port}"


def _ask(sock: Path, request: bytes) -> bytes:
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(10)
    client.connect(str(sock))
    with client:
        client.sendall(request)
        reply = b""
        while chunk := client.recv(4096):
            reply += chunk
    return reply


def _post(path: str = "/v1/chat/completions", auth: str = "Bearer dummy") -> bytes:
    body = b'{"model":"local","messages":[]}'
    return (
        f"POST {path} HTTP/1.1\r\nAuthorization: {auth}\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


@needs_sandbox
def test_no_host_port_is_reachable_and_the_bridge_works(
    tmp_path: Path, fake_litellm: FakeLiteLLM
) -> None:
    root, work, scratch, gitdir = (tmp_path / n for n in ("root", "work", "scratch", "gitdir"))
    for folder in (root / ".git", work, scratch / "history", gitdir):
        folder.mkdir(parents=True)
    (scratch / "probe.py").write_text(PROBE)
    sock = scratch / "sock" / "llm.sock"
    ports = ",".join(str(p) for p in (*HOST_PORTS, fake_litellm.port))
    with bridge.unix_bridge(sock, _url(fake_litellm), REAL_KEY):
        done = subprocess.run(  # noqa: S603 - the dispatcher's own sandbox argv
            [
                *local.sandbox_args(root, work, scratch, gitdir),
                "/usr/bin/python3",
                "-I",
                str(scratch / "probe.py"),
                str(sock),
                ports,
            ],
            env=local.sandbox_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert done.returncode == 0, done.stderr
    assert "REACHED" not in done.stdout, done.stdout
    assert f"BLOCKED {ports}" in done.stdout
    assert REAL_KEY not in done.stdout  # not in the sandbox's environment
    assert "CHAT HTTP/1.1 200 OK" in done.stdout
    assert "ADMIN HTTP/1.1 403 Forbidden" in done.stdout
    assert fake_litellm.seen == [("POST", "/v1/chat/completions", f"Bearer {REAL_KEY}")]


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


def test_model_socket_is_private(tmp_path: Path, fake_litellm: FakeLiteLLM) -> None:
    sock = tmp_path / "sock" / "llm.sock"
    with bridge.unix_bridge(sock, _url(fake_litellm), REAL_KEY):
        assert sock.stat().st_mode & 0o077 == 0
        assert (sock.parent.stat().st_mode & 0o777) == 0o700
    assert not sock.exists()


def test_bridge_caps_connections_and_closes_them_on_exit(
    tmp_path: Path, fake_litellm: FakeLiteLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "MAX_CONNECTIONS", 2)
    sock = tmp_path / "sock" / "llm.sock"
    held: list[socket.socket] = []
    with bridge.unix_bridge(sock, _url(fake_litellm), REAL_KEY):
        for _ in range(3):
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            client.connect(str(sock))
            held.append(client)
        assert held[2].recv(10) == b""  # the third is closed at once
        held[0].sendall(_post())
        assert held[0].recv(15) == b"HTTP/1.1 200 OK"
    assert held[1].recv(10) == b""  # still open at exit: closed by the bridge
    for client in held:
        client.close()


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"GET /key/info HTTP/1.1\r\n\r\n",
        b"POST /key/generate HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}",
        b"GET /v1/models HTTP/1.1\r\n\r\n",
        b"POST /chat/completions HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}",
        b"POST /v1/chat/completions/../../key/info HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}",
        b"DELETE /v1/chat/completions HTTP/1.1\r\n\r\n",
    ],
    ids=["key-info", "key-generate", "models", "no-v1", "dot-dot", "delete"],
)
def test_admin_and_other_routes_are_refused(
    tmp_path: Path, fake_litellm: FakeLiteLLM, request_bytes: bytes
) -> None:
    sock = tmp_path / "sock" / "llm.sock"
    with bridge.unix_bridge(sock, _url(fake_litellm), REAL_KEY):
        reply = _ask(sock, request_bytes)
    assert reply.startswith(b"HTTP/1.1 403 Forbidden")
    assert fake_litellm.seen == []  # never reached LiteLLM


def test_chat_gets_the_real_key_and_never_the_sandbox_one(
    tmp_path: Path, fake_litellm: FakeLiteLLM
) -> None:
    sock = tmp_path / "sock" / "llm.sock"
    with bridge.unix_bridge(sock, _url(fake_litellm), REAL_KEY):
        reply = _ask(sock, _post(auth=f"Bearer {bridge.SANDBOX_KEY}"))
    assert reply.startswith(b"HTTP/1.1 200 OK") and reply.endswith(b'"pong"}}]}')
    assert fake_litellm.seen == [("POST", "/v1/chat/completions", f"Bearer {REAL_KEY}")]


@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (b"POST /v1/chat/completions HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", b"411"),
        (b"POST /v1/chat/completions HTTP/1.1\r\n\r\n", b"411"),
        (b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 99999999\r\n\r\n", b"413"),
        (b"nonsense\r\n\r\n", b"400"),
    ],
)
def test_malformed_requests_are_refused(
    tmp_path: Path, fake_litellm: FakeLiteLLM, request_bytes: bytes, status: bytes
) -> None:
    sock = tmp_path / "sock" / "llm.sock"
    with bridge.unix_bridge(sock, _url(fake_litellm), REAL_KEY):
        assert _ask(sock, request_bytes).split(b" ")[1] == status
    assert fake_litellm.seen == []


def test_sandbox_env_holds_no_real_key() -> None:
    env = local.sandbox_env()
    assert env["OPENAI_API_KEY"] == bridge.SANDBOX_KEY
    assert "OPENAI_API_BASE" not in env  # the driver points at its own forwarder
