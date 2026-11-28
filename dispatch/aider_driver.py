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
"""

import json
import sys
from pathlib import Path


def main(config_path: str) -> int:
    from aider.analytics import Analytics  # type: ignore[import-not-found]
    from aider.coders import Coder  # type: ignore[import-not-found]
    from aider.io import InputOutput  # type: ignore[import-not-found]
    from aider.models import Model, register_models  # type: ignore[import-not-found]
    from aider.repo import GitRepo  # type: ignore[import-not-found]

    config = json.loads(Path(config_path).read_text("utf-8"))
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
