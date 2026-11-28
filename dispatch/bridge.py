"""The only way out of Aider's sandbox: a Unix socket in front of LiteLLM (ADR-030
step 4b, #202, #210).

Aider's sandbox has no network at all (bubblewrap ``--unshare-all``). Inside it,
``aider_driver.py`` listens on the sandbox's own loopback and pumps bytes to this
socket. Here, outside the sandbox, the bridge:

-   reads one HTTP request per connection and forwards only
    ``POST /v1/chat/completions`` to the LiteLLM URL from the settings (checked
    again here: http, loopback). Any other method or path gets 403 and never
    reaches LiteLLM (no admin routes, no key or model management);
-   checks the body too, because LiteLLM obeys request params (``api_base``,
    ``user_config``, ``fallbacks``, logging params...) that could send the call,
    with the master key, somewhere else. Only model ``local`` and the keys Aider
    0.86 sends are allowed, and the bridge forwards its own re-encoding of the
    parsed body, never the original bytes;
-   drops every header Aider sent and adds the real LiteLLM key itself. Aider
    only ever holds a dummy key; the real one never enters the sandbox's
    environment or files;
-   sends back only the status, ``Content-Type`` and the body, with the key
    masked if LiteLLM ever echoed it.

Standard library only (no socat): at most ``MAX_CONNECTIONS`` at once, idle ones
dropped, all closed when the try ends.
"""

import contextlib
import http.client
import ipaddress
import json
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

BACKLOG = 8
CONNECT_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 300.0  # longer than the slowest local-model reply (stream off)
MAX_CONNECTIONS = 16  # Aider uses one or two; more are closed at once
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 16 * 1024 * 1024
# The only request that leaves the sandbox. Aider 0.86 sends nothing else to the
# model API (checked with a live run, ADR-030).
ALLOWED = frozenset({("POST", "/v1/chat/completions")})
# The body keys Aider 0.86 sends (recorded from a live run, ADR-030); anything else,
# such as api_base, api_key, user_config, fallbacks, metadata or tags, gets 403.
MODEL = "local"
BODY_KEYS = frozenset({"model", "messages", "temperature", "chat_template_kwargs"})
MESSAGE_KEYS = frozenset({"role", "content"})
ROLES = frozenset({"system", "user", "assistant"})
TEMPLATE_KWARGS = {"enable_thinking": False}
SANDBOX_KEY = "sk-dispatch-sandbox-dummy"  # what Aider holds; not a credential


class Refused(Exception):
    """A request the bridge won't forward. The message is the HTTP status line."""


def target(url: str) -> tuple[str, int]:
    """Host and port of the LiteLLM URL: plain http to a loopback IP only, so the
    socket can never become a way out to another machine."""
    parts = urlsplit(url)
    host = "127.0.0.1" if parts.hostname == "localhost" else parts.hostname
    try:
        loopback = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "http" or not loopback:
        raise ValueError("the model bridge forwards to an http:// loopback IP only")
    return str(host), parts.port or 80


def _read_head(client: socket.socket) -> tuple[list[str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            raise Refused("400 Bad Request")
        data += chunk
        if len(data) > MAX_HEADER_BYTES:
            raise Refused("431 Request Header Fields Too Large")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("latin-1").split("\r\n"), rest


def _content_length(lines: list[str]) -> int:
    headers = {}
    for line in lines:
        name, sep, value = line.partition(":")
        key = name.strip().lower()
        if not sep or key in headers:  # no duplicate headers (Content-Length smuggling)
            raise Refused("400 Bad Request")
        headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise Refused("411 Length Required")
    try:
        length = int(headers.get("content-length", ""))
    except ValueError:
        raise Refused("411 Length Required") from None
    if not 0 <= length <= MAX_BODY_BYTES:
        raise Refused("413 Content Too Large")
    return length


def read_request(client: socket.socket) -> bytes:
    """The body of an allowed request; ``Refused`` for anything else."""
    lines, body = _read_head(client)
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise Refused("400 Bad Request")
    if (parts[0], parts[1]) not in ALLOWED:
        raise Refused("403 Forbidden")
    length = _content_length(lines[1:])
    while len(body) < length:
        chunk = client.recv(min(65536, length - len(body)))
        if not chunk:
            raise Refused("400 Bad Request")
        body += chunk
    return body[:length]


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Refused("403 Forbidden")
        result[key] = value
    return result


def _content_ok(content: object) -> bool:
    if isinstance(content, str):
        return True
    return isinstance(content, list) and all(
        isinstance(part, dict)
        and set(part) == {"type", "text"}
        and part["type"] == "text"
        and isinstance(part["text"], str)
        for part in content
    )


def check_body(body: bytes) -> bytes:
    """The re-encoded body if it is a plain chat call to model ``local`` with only
    Aider's keys; ``Refused`` otherwise."""
    try:
        data = json.loads(body, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError):
        raise Refused("400 Bad Request") from None
    if not isinstance(data, dict) or not set(data) <= BODY_KEYS or data.get("model") != MODEL:
        raise Refused("403 Forbidden")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Refused("403 Forbidden")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != MESSAGE_KEYS
            or message["role"] not in ROLES
            or not _content_ok(message["content"])
        ):
            raise Refused("403 Forbidden")
    temperature = data.get("temperature", 0)
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        raise Refused("403 Forbidden")
    if data.get("chat_template_kwargs", TEMPLATE_KWARGS) != TEMPLATE_KWARGS:
        raise Refused("403 Forbidden")
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _reply(client: socket.socket, status: str, content_type: str, body: bytes) -> None:
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    client.sendall(head.encode("latin-1") + body)


def forward(host: str, port: int, key: str, body: bytes) -> tuple[str, str, bytes]:
    """POST the body to LiteLLM with the real key. Returns status, type, body."""
    conn = http.client.HTTPConnection(host, port, timeout=IDLE_TIMEOUT_S)
    try:
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = conn.getresponse()
        reply = response.read(MAX_REPLY_BYTES + 1)
        if len(reply) > MAX_REPLY_BYTES:
            return "502 Bad Gateway", "text/plain", b""
        content_type = response.getheader("Content-Type", "application/json")
        status = f"{response.status} {response.reason or 'Status'}"
        return status, content_type, reply.replace(key.encode(), b"***")
    finally:
        conn.close()


class _Open:
    """The bridge's open client sockets, so all of them close when the try ends."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sockets: set[socket.socket] = set()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def add(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.add(sock)

    def discard(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.discard(sock)

    def close_all(self) -> None:
        with self.lock:
            open_now = list(self.sockets)
        for sock in open_now:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _serve_one(client: socket.socket, host: str, port: int, key: str, opened: _Open) -> None:
    opened.add(client)
    try:
        with client:
            client.settimeout(IDLE_TIMEOUT_S)
            try:
                body = check_body(read_request(client))
            except Refused as refused:
                with contextlib.suppress(OSError):
                    _reply(client, str(refused), "text/plain", b"")
                return
            except OSError:
                return
            try:
                status, content_type, reply = forward(host, port, key, body)
            except (OSError, http.client.HTTPException):
                status, content_type, reply = "502 Bad Gateway", "text/plain", b""
            with contextlib.suppress(OSError):
                _reply(client, status, content_type, reply)
    finally:
        opened.discard(client)
        opened.slots.release()


@contextlib.contextmanager
def unix_bridge(path: Path, url: str, key: str) -> Iterator[Path]:
    """Listen on ``path`` (mode 600) and forward allowed requests to ``url`` with
    ``key``: at most ``MAX_CONNECTIONS`` at once, all closed on exit."""
    host, port = target(url)
    opened = _Open()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        server.bind(str(path))
    finally:
        os.umask(old)
    server.listen(BACKLOG)
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except OSError:
                return
            if not opened.slots.acquire(blocking=False):
                client.close()  # over the limit
                continue
            threading.Thread(
                target=_serve_one, args=(client, host, port, key, opened), daemon=True
            ).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        stop.set()
        with contextlib.suppress(OSError):
            server.shutdown(socket.SHUT_RDWR)
        server.close()
        thread.join(timeout=5)
        opened.close_all()
        path.unlink(missing_ok=True)
