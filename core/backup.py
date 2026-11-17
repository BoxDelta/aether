"""Database backups (#3, ADR-025): a verified ``pg_dump`` with D5 retention.

One run (fish)::

    uv run python -m core.backup

What a run does, in order:

1.  Checks the backup folder (``AETHER_BACKUP_DIR``, default
    ``~/.local/share/aether/backups``): absolute, outside the repository, and
    private (no group or other access; created with mode 700 if missing). Takes
    a lock in it, so two runs never overlap, and removes partial files left by
    an interrupted run.
2.  Runs ``pg_dump`` (custom format) **inside** the Postgres container, as the
    owner, over the container's local socket (``docker exec``). No password is
    needed or passed: none is on the command line, in the environment, in the
    process list or in any log. The dump streams into a temporary file
    (mode 600) in the backup folder.
3.  Verifies it: non-empty, and ``pg_restore --list`` (again inside the
    container) reads the whole archive and lists at least one table's data.
4.  Only then renames it to ``<database>-<UTC time>.dump`` and prunes old
    backups by the retention rule (``keep``). A failed, empty or unreadable
    dump is deleted and never replaces, or causes the pruning of, a good one.

Failures exit non-zero with one log line: the stage, the exit code or error
type. Never a path beyond the file name, a URL, a password or tool output
(``pg_dump``'s messages could name hosts or objects, so they are dropped).
Logs are JSON lines through core.log, so under systemd they land in the journal.

Restore: docs/runbooks/db-backup-restore.md.
"""

import argparse
import contextlib
import fcntl
import logging
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from core.config import Settings, get_settings
from core.log import configure_logging

logger = logging.getLogger("aether.backup")

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "~/.local/share/aether/backups"
# docker-compose.yml: project "aether", service "postgres".
DEFAULT_CONTAINER = "aether-postgres-1"
DEFAULT_DATABASE = "aether"
DEFAULT_USER = "aether"  # the owner (POSTGRES_USER), trusted on the local socket

# ADR-024 D5 (accepted) and ADR-025: 7 daily plus 4 weekly.
KEEP_DAILY = 7
KEEP_WEEKLY = 4

DUMP_TIMEOUT_S = 30 * 60
VERIFY_TIMEOUT_S = 10 * 60

_NAME = r"[a-z_][a-z0-9_]{0,62}"
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_BACKUP_FILE = re.compile(rf"(?P<db>{_NAME})-(?P<ts>\d{{8}}T\d{{6}}Z)\.dump")
_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
PARTIAL_SUFFIX = ".partial"
LOCK_FILE = ".lock"


class BackupError(Exception):
    """A run failed at ``stage``. The message is safe to log: no path or secret."""

    def __init__(self, stage: str, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class Backup:
    path: Path
    database: str
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class Result:
    file_name: str
    size_bytes: int
    kept: int
    pruned: int


# --- retention --------------------------------------------------------------------


def parse_backup(path: Path) -> Backup | None:
    """The backup this file name describes, or None for any other file."""
    match = _BACKUP_FILE.fullmatch(path.name)
    if match is None:
        return None
    try:
        taken = datetime.strptime(match["ts"], _TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return Backup(path, match["db"], taken)


def list_backups(folder: Path, database: str) -> list[Backup]:
    """Completed backups of ``database`` in ``folder``, oldest first. Partial files
    never match the name pattern, so they are never listed (or kept, or pruned)."""
    found = [parse_backup(p) for p in folder.iterdir() if p.is_file()]
    return sorted(
        (b for b in found if b is not None and b.database == database),
        key=lambda b: b.taken_at,
    )


def keep(
    backups: Iterable[Backup], *, daily: int = KEEP_DAILY, weekly: int = KEEP_WEEKLY
) -> set[Path]:
    """Which backups to keep (UTC):

    - daily: the newest backup of each of the ``daily`` most recent days that have
      one;
    - weekly: the newest backup of each of the ``weekly`` most recent ISO weeks
      that are older than the week of the oldest daily backup.

    So with a backup every day, the oldest kept is 4 to 5 weeks old, and a
    deleted record is gone from every backup after about 5 weeks (ADR-024 D5).
    The newest backup is always kept. Counted by days and weeks that *have*
    backups, not by the calendar: if backups stop, the last ones are kept, never
    aged out to nothing.
    """
    newest_first = sorted(backups, key=lambda b: b.taken_at, reverse=True)
    kept: set[Path] = set()
    days: set[object] = set()
    for backup in newest_first:
        day = backup.taken_at.date()
        if day not in days and len(days) < daily:
            days.add(day)
            kept.add(backup.path)
    oldest_daily_week = (
        min(b.taken_at for b in newest_first if b.path in kept).isocalendar()[:2] if kept else None
    )
    weeks: set[object] = set()
    for backup in newest_first:
        week = backup.taken_at.isocalendar()[:2]
        if oldest_daily_week is not None and week >= oldest_daily_week:
            continue
        if week not in weeks and len(weeks) < weekly:
            weeks.add(week)
            kept.add(backup.path)
    if newest_first:
        kept.add(newest_first[0].path)
    return kept


def prune(folder: Path, database: str) -> tuple[int, int]:
    """Delete backups of ``database`` that ``keep`` doesn't keep. Returns
    (kept, pruned). Called only after a new backup was verified."""
    backups = list_backups(folder, database)
    kept = keep(backups)
    pruned = 0
    for backup in backups:
        if backup.path not in kept:
            backup.path.unlink()
            pruned += 1
    return len(kept), pruned


# --- the folder -------------------------------------------------------------------


def backup_dir(settings: Settings) -> Path:
    """The configured folder. Raises ``BackupError`` (without the value) if unusable."""
    raw = settings.aether_backup_dir or DEFAULT_DIR
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise BackupError("config", "AETHER_BACKUP_DIR must be an absolute path")
    resolved = folder.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise BackupError("config", "AETHER_BACKUP_DIR must be outside the repository")
    return resolved


def prepare_dir(folder: Path) -> None:
    """Create the folder (mode 700) if needed; refuse one others can read or enter."""
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = folder.stat().st_mode
    except OSError as exc:
        raise BackupError(
            "folder", f"cannot use the backup folder ({type(exc).__name__})"
        ) from None
    if not stat.S_ISDIR(mode):
        raise BackupError("folder", "the backup folder is not a directory")
    if mode & 0o077:
        raise BackupError("folder", "the backup folder must be private to its owner (chmod 700)")


@contextlib.contextmanager
def locked(folder: Path) -> Iterator[None]:
    fd = os.open(folder / LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("lock", "another backup is running") from None
        yield
    finally:
        os.close(fd)  # releases the lock


def remove_partials(folder: Path) -> None:
    """Partial files left by an interrupted run (we hold the lock, so none is live)."""
    for path in folder.iterdir():
        if path.name.endswith(PARTIAL_SUFFIX) and path.is_file():
            path.unlink()


# --- running the tools in the container ----------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _run(
    argv: Sequence[str],
    *,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | int | None = None,
    timeout: float,
) -> "subprocess.CompletedProcess[bytes]":
    # An argument list, never a shell. stderr is captured and dropped: tool messages
    # are never logged (they can name hosts, roles or objects).
    return subprocess.run(  # noqa: S603 - fixed argv, validated names
        list(argv),
        stdin=stdin if stdin is not None else subprocess.DEVNULL,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def dump_argv(container: str, database: str, user: str) -> list[str]:
    """``pg_dump`` inside the container over its local socket. No password, no URL."""
    return [
        "docker", "exec", "-i", container,
        "pg_dump", "--format=custom", "--no-password", f"--username={user}",
        f"--dbname={database}",
    ]  # fmt: skip


def list_argv(container: str) -> list[str]:
    """``pg_restore --list`` inside the container, reading the archive from stdin."""
    return ["docker", "exec", "-i", container, "pg_restore", "--list"]


def _check_name(kind: str, value: str, pattern: re.Pattern[str]) -> None:
    if not pattern.fullmatch(value):
        raise BackupError("config", f"invalid {kind} name")


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_backup(  # noqa: PLR0913 - keyword-only options, all defaulted
    folder: Path,
    *,
    container: str = DEFAULT_CONTAINER,
    database: str = DEFAULT_DATABASE,
    user: str = DEFAULT_USER,
    now: datetime | None = None,
    runner: Runner = _run,
    prune_old: bool = True,
) -> Result:
    """One verified backup, then pruning. Raises ``BackupError`` on any failure,
    after removing its own partial file; existing backups are left untouched."""
    _check_name("container", container, _CONTAINER)
    _check_name("database", database, re.compile(_NAME))
    _check_name("user", user, re.compile(_NAME))
    prepare_dir(folder)
    taken = (now or datetime.now(UTC)).astimezone(UTC)
    name = f"{database}-{taken.strftime(_TIME_FORMAT)}.dump"
    final = folder / name
    partial = folder / f".{name}{PARTIAL_SUFFIX}"

    with locked(folder):
        remove_partials(folder)
        if final.exists():
            raise BackupError("name", "a backup with this time stamp already exists")
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                try:
                    done = runner(
                        dump_argv(container, database, user), stdout=out, timeout=DUMP_TIMEOUT_S
                    )
                except subprocess.TimeoutExpired:
                    raise BackupError("dump", "pg_dump timed out") from None
                except OSError as exc:
                    raise BackupError("dump", f"cannot run docker ({type(exc).__name__})") from None
                if done.returncode != 0:
                    raise BackupError("dump", "pg_dump failed", exit_code=done.returncode)
                out.flush()
                os.fsync(out.fileno())
            size = partial.stat().st_size
            if size == 0:
                raise BackupError("verify", "the dump is empty")
            with partial.open("rb") as archive:
                try:
                    listed = runner(list_argv(container), stdin=archive, timeout=VERIFY_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    raise BackupError("verify", "pg_restore --list timed out") from None
            if listed.returncode != 0:
                raise BackupError(
                    "verify", "pg_restore cannot read the dump", exit_code=listed.returncode
                )
            if b" TABLE DATA " not in (listed.stdout or b""):
                raise BackupError("verify", "the dump holds no table data")
            os.replace(partial, final)
            _fsync_dir(folder)
        finally:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
        if prune_old:
            kept, pruned = prune(folder, database)
        else:  # e.g. a safety backup before a restore: never prune the one being restored
            kept, pruned = len(list_backups(folder, database)), 0
    return Result(name, size, kept, pruned)


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.backup", description=__doc__)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="keep every existing backup (use before a restore)",
    )
    args = parser.parse_args(argv)
    settings = settings if settings is not None else get_settings()
    configure_logging(settings)
    try:
        folder = backup_dir(settings)
        result = run_backup(
            folder,
            container=args.container,
            database=args.database,
            user=args.user,
            prune_old=not args.no_prune,
        )
    except BackupError as exc:
        logger.error(
            "Backup failed: %s",
            exc,
            extra={"stage": exc.stage, "exit_code": exc.exit_code, "database": args.database},
        )
        return 1
    except Exception as exc:  # one safe line, never a traceback with paths
        logger.error(
            "Backup failed",
            extra={"stage": "unexpected", "error_type": type(exc).__name__},
        )
        return 1
    logger.info(
        "Backup completed",
        extra={
            "backup_file": result.file_name,
            "size_bytes": result.size_bytes,
            "kept": result.kept,
            "pruned": result.pruned,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
