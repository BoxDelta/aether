"""A Unix socket that forwards to LiteLLM on loopback (ADR-030 step 4b, #202).

Aider's sandbox has no network at all (bubblewrap ``--unshare-net``). Its only way
out is this socket, bound into the sandbox, which reaches exactly one address: the
LiteLLM URL from the dispatcher's settings (checked again here: http, loopback). Inside the
sandbox, ``aider_driver.py`` listens on the sandbox's own loopback and forwards to
the socket, so Aider still talks plain HTTP.

Standard library only (no socat), one thread per connection, stopped when the try
ends.
"""

import contextlib
import ipaddress
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

CHUNK = 65536
BACKLOG = 8
CONNECT_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 300.0  # longer than the slowest local-model reply (stream off)
MAX_CONNECTIONS = 16  # Aider uses one or two; more are closed at once


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


def pump(source: socket.socket, sink: socket.socket) -> None:
    """Copy one direction until it closes, then half-close the other side."""
    try:
        while data := source.recv(CHUNK):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)


class _Open:
    """The bridge's open sockets, so all of them close when the try ends."""

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


def _serve_one(client: socket.socket, host: str, port: int, opened: _Open) -> None:
    opened.add(client)
    try:
        with client:
            client.settimeout(IDLE_TIMEOUT_S)
            try:
                upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
            except OSError:
                return
            opened.add(upstream)
            with upstream:
                upstream.settimeout(IDLE_TIMEOUT_S)
                back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
                back.start()
                pump(client, upstream)
                back.join()
            opened.discard(upstream)
    finally:
        opened.discard(client)
        opened.slots.release()


@contextlib.contextmanager
def unix_bridge(path: Path, url: str) -> Iterator[Path]:
    """Listen on ``path`` (mode 600) and forward each connection to ``url``: at most
    ``MAX_CONNECTIONS`` at once, idle ones dropped, all closed on exit."""
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
                target=_serve_one, args=(client, host, port, opened), daemon=True
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
