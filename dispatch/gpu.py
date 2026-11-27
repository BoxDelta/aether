"""Is the GPU held only by our own model? (ADR-030 §Models, #131)

llama-swap keeps the local model loaded (about 7.4 GB of 8 GB) until it has been
idle for a while, so free VRAM alone can't tell "our model is loaded" from "the
desktop is busy". This asks ``nvidia-smi`` which compute processes hold the GPU.
It never raises: any failure means "unknown", and the caller falls back to the
normal headroom check.
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import PurePosixPath

OUR_PROCESS = "llama-server"
TIMEOUT_S = 2.0
QUERY_ARGS = ("--query-compute-apps=process_name", "--format=csv,noheader")

Runner = Callable[[list[str]], str]


def _run(args: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - absolute path from which(), fixed args
        args, capture_output=True, text=True, timeout=TIMEOUT_S, check=True
    )
    return result.stdout


def parse_apps(output: str) -> list[str]:
    """Process base names, one per non-empty line."""
    names = []
    for line in output.splitlines():
        name = line.strip()
        if name:
            names.append(PurePosixPath(name).name)
    return names


def only_our_model(runner: Runner = _run) -> bool | None:
    """True if every GPU compute process is llama-server; False if another process
    (or none) holds the GPU; None if nvidia-smi is missing or fails."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        names = parse_apps(runner([exe, *QUERY_ARGS]))
    except Exception:  # never raise: unknown means "check headroom as now"
        return None
    return bool(names) and all(name == OUR_PROCESS for name in names)
