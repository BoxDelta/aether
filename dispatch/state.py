"""What the dispatcher has done per issue, and one run at a time (ADR-030).

``.dispatch/state.json`` (git-ignored, written atomically) keeps three sets:

- ``briefed``: a Claude brief was written; the issue is never sorted again.
- ``announced``: Discord accepted "#<n> brief ready"; never posted again.
- ``local``: sorted to the local path, which picks it up (step 4b).

The dry run's ``posted`` key (step 4a) is read and dropped: those issues are sorted
again, because a dry-run post did nothing with them.
"""

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

DISPATCH_DIR = Path(__file__).resolve().parents[1] / ".dispatch"
STATE_FILE = DISPATCH_DIR / "state.json"
LOCK_FILE = DISPATCH_DIR / "run.lock"
KEYS = ("briefed", "announced", "local")
LEGACY_KEYS = frozenset({"posted"})


class StateError(Exception):
    """The state file can't be read. The run stops rather than repeating work."""


class Busy(Exception):
    """Another run holds the lock."""


@dataclass
class State:
    briefed: set[int] = field(default_factory=set)
    announced: set[int] = field(default_factory=set)
    local: set[int] = field(default_factory=set)

    def handled(self) -> frozenset[int]:
        """Issues a run skips before the trust check, the rules and the model."""
        return frozenset(self.briefed | self.local)


@contextmanager
def run_lock(path: Path = LOCK_FILE) -> Iterator[None]:
    """One run at a time (timer and manual). Non-blocking: a second run stops at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _numbers(value: object, path: Path) -> set[int]:
    if not isinstance(value, list) or not all(
        isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in value
    ):
        raise StateError(f"state file malformed: {path}")
    return set(value)


def load(path: Path = STATE_FILE) -> State:
    """A missing file is an empty state. Unknown keys or bad values are an error."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return State()
    except (OSError, ValueError) as exc:
        raise StateError(f"state file unreadable: {path}") from exc
    if not isinstance(data, dict) or not set(data) <= set(KEYS) | LEGACY_KEYS:
        raise StateError(f"state file malformed: {path}")
    for key in LEGACY_KEYS & set(data):
        _numbers(data[key], path)  # still validated, then dropped
    return State(**{key: _numbers(data.get(key, []), path) for key in KEYS})


def save(state: State, path: Path = STATE_FILE) -> None:
    """Atomic: write a temp file in the same folder, fsync, then rename over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({key: sorted(getattr(state, key)) for key in KEYS}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
