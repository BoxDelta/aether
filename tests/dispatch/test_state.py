"""The dispatcher's state file and run lock (ADR-030)."""

import json
from pathlib import Path

import pytest

from dispatch import state


def test_round_trip_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "state.json"
    assert state.load(path) == state.State()
    state.save(state.State(briefed={3, 1}, announced={1}, local={7}), path)
    assert json.loads(path.read_text()) == {"briefed": [1, 3], "announced": [1], "local": [7]}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]  # no temp left
    assert state.load(path).handled() == {1, 3, 7}


def test_dry_run_posted_key_is_dropped(tmp_path: Path) -> None:
    # Issues the 4a dry run posted get sorted again: a dry-run post did nothing.
    path = tmp_path / "state.json"
    path.write_text('{"posted": [5, 6]}')
    assert state.load(path) == state.State()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"briefed": "x"}',
        '{"briefed": [true]}',
        '{"briefed": [0]}',
        '{"posted": ["1"]}',
        '{"surprise": []}',
    ],
)
def test_bad_state_is_an_error(tmp_path: Path, text: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(text)
    with pytest.raises(state.StateError):
        state.load(path)


def test_files_are_at_repo_root() -> None:
    root = Path(__file__).resolve().parents[2]
    assert state.STATE_FILE.relative_to(root) == Path(".dispatch/state.json")
    assert state.LOCK_FILE.relative_to(root) == Path(".dispatch/run.lock")


def test_second_run_is_refused(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with state.run_lock(lock), pytest.raises(state.Busy), state.run_lock(lock):
        pass
    with state.run_lock(lock):  # released after the first run
        pass
