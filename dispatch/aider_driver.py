"""Runs one Aider edit through Aider's scripting API (ADR-030 step 4b).

Started by ``local.py`` inside bubblewrap with Aider's own Python
(``uv tool install aider-chat``), never with the project's environment, so it is
not imported anywhere in Aether. Arguments: a JSON file with ``message``,
``files``, ``model``, ``map_tokens``, ``history`` and ``settings``.

Why a driver instead of the ``aider`` command: with ``--yes-always`` Aider adds
every file the model mentions and re-sends before applying the edit, which
overflows the 16k context of the local model and drops the edit. Here a mention
never adds a file, and only the files the issue names are in the chat.
Checked against aider-chat 0.86.2.

The sandbox has no network (#202). The driver listens on the sandbox's own
loopback and forwards each connection to the Unix socket the dispatcher bound in
(``model_socket``), which reaches only LiteLLM. Aider's model calls go to that
loopback port.
"""

import contextlib
import json
import os
import socket
import sys
import threading
from pathlib import Path

CHUNK = 65536
IDLE_TIMEOUT_S = 300.0
MAX_CONNECTIONS = 16
_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(CHUNK):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)


def _serve(client: socket.socket, socket_path: str) -> None:
    try:
        with client, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            client.settimeout(IDLE_TIMEOUT_S)
            upstream.settimeout(IDLE_TIMEOUT_S)
            try:
                upstream.connect(socket_path)
            except OSError:
                return
            back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
            back.start()
            _pump(client, upstream)
            back.join()
    finally:
        _slots.release()


def start_forwarder(socket_path: str) -> int:
    """127.0.0.1:<free port> inside the sandbox -> the model socket. Returns the port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def accept() -> None:
        while True:
            client, _ = server.accept()
            if not _slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=_serve, args=(client, socket_path), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return int(server.getsockname()[1])


def main(config_path: str) -> int:
    config = json.loads(Path(config_path).read_text("utf-8"))
    port = start_forwarder(config["model_socket"])
    os.environ["OPENAI_API_BASE"] = f"http://127.0.0.1:{port}"  # before litellm loads

    from aider.analytics import Analytics  # type: ignore[import-not-found]
    from aider.coders import Coder  # type: ignore[import-not-found]
    from aider.io import InputOutput  # type: ignore[import-not-found]
    from aider.models import Model, register_models  # type: ignore[import-not-found]
    from aider.repo import GitRepo  # type: ignore[import-not-found]

    history = Path(config["history"])
    register_models([str(Path(config["settings"]))])
    io = InputOutput(
        yes=True,
        pretty=False,
        fancy_input=False,
        chat_history_file=str(history / "chat.md"),
        input_history_file=str(history / "input.txt"),
    )
    model = Model(config["model"])
    repo = GitRepo(io, config["files"], ".")
    coder = Coder.create(
        main_model=model,
        io=io,
        repo=repo,
        fnames=config["files"],
        auto_commits=False,
        dirty_commits=False,
        map_tokens=config["map_tokens"],
        suggest_shell_commands=False,
        detect_urls=False,
        auto_lint=False,
        auto_test=False,
        stream=False,
        analytics=Analytics(permanently_disable=True),
    )
    coder.check_for_file_mentions = lambda _content: None  # never add files mid-edit
    coder.run(with_message=config["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
