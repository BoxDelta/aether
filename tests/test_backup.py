"""core.backup without Docker or a database (#3): retention, the run's safety rules,
and what a failure logs. The live round trip is in tests/test_backup_live.py."""

import io
import json
import logging
import os
import stat
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pytest

import core.backup
from core.backup import (
    Backup,
    BackupError,
    backup_dir,
    dump_argv,
    keep,
    list_backups,
    parse_backup,
    prune,
    run_backup,
)
from core.config import Settings
from core.log import configure_logging

CANARY_PASSWORD = "canary-backup-password-7c1f0e"  # noqa: S105  # gitleaks:allow
CANARY_URL = f"postgresql+asyncpg://aether:{CANARY_PASSWORD}@127.0.0.1:5432/aether"
NOW = datetime(2026, 11, 16, 3, 30, tzinfo=UTC)  # a Monday
TOC = b";\n; Archive created\n3355; 0 16390 TABLE DATA public events aether\n"


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def backups(*times: datetime, db: str = "aether") -> list[Backup]:
    return [Backup(Path(f"/b/{db}-{t.strftime('%Y%m%dT%H%M%SZ')}.dump"), db, t) for t in times]


# --- retention (fake dates) ---------------------------------------------------------


def test_daily_backups_for_a_long_time_keep_7_daily_and_4_weekly() -> None:
    daily = backups(*(NOW - timedelta(days=d) for d in range(60)))
    kept = sorted(b.taken_at for b in daily if b.path in keep(daily))
    # 7 most recent days, then the newest backup of each of the 4 ISO weeks before
    # the oldest daily one's week (2026-11-10 is in week 46).
    assert [k.date().isoformat() for k in kept] == [
        "2026-10-18",  # Sunday, week 42
        "2026-10-25",  # week 43
        "2026-11-01",  # week 44
        "2026-11-08",  # week 45
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-14",
        "2026-11-15",
        "2026-11-16",
    ]


@pytest.mark.parametrize("days_later", range(14))
def test_oldest_kept_is_never_more_than_5_weeks_old(days_later: int) -> None:
    now = NOW + timedelta(days=days_later)
    daily = backups(*(now - timedelta(days=d) for d in range(90)))
    oldest = min(b.taken_at for b in daily if b.path in keep(daily))
    assert timedelta(weeks=4) <= now - oldest <= timedelta(days=35)


def test_several_backups_a_day_keep_the_newest_of_the_day() -> None:
    times = [NOW - timedelta(hours=h) for h in (0, 1, 2)]
    kept = keep(backups(*times))
    assert kept == {backups(NOW)[0].path}


def test_newest_is_always_kept_even_with_zero_slots() -> None:
    items = backups(NOW, NOW - timedelta(days=1))
    assert keep(items, daily=0, weekly=0) == {items[0].path}


def test_backups_that_stopped_are_not_aged_out() -> None:
    # Counted by days that have backups, not by the calendar: a year-old set stays.
    old = backups(*(NOW - timedelta(days=365 + d) for d in range(3)))
    assert keep(old) == {b.path for b in old}


def test_empty_set() -> None:
    assert keep([]) == set()


def test_parse_backup_names() -> None:
    assert parse_backup(Path("aether-20261116T033000Z.dump")) == Backup(
        Path("aether-20261116T033000Z.dump"), "aether", NOW
    )
    for name in (
        ".aether-20261116T033000Z.dump.partial",
        "aether-20261116T033000Z.dump.bak",
        "aether-20261399T033000Z.dump",  # not a date
        "notes.txt",
        "Aether-20261116T033000Z.dump",
    ):
        assert parse_backup(Path(name)) is None, name


def test_prune_only_touches_this_databases_backups(tmp_path: Path) -> None:
    for d in range(20):
        t = (NOW - timedelta(days=d)).strftime("%Y%m%dT%H%M%SZ")
        (tmp_path / f"aether-{t}.dump").write_bytes(b"x")
        (tmp_path / f"other-{t}.dump").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("keep me")
    kept, pruned = prune(tmp_path, "aether")
    assert (kept, pruned) == (len(list_backups(tmp_path, "aether")), 20 - kept)
    assert len(list_backups(tmp_path, "other")) == 20
    assert (tmp_path / "unrelated.txt").exists()


# --- a run, with a fake docker ---------------------------------------------------------


class FakeDocker:
    """Records argv; pg_dump writes ``dump``; pg_restore --list answers ``toc``."""

    def __init__(
        self,
        *,
        dump: bytes = b"PGDMP fake archive",
        dump_rc: int = 0,
        toc: bytes = TOC,
        list_rc: int = 0,
        read_rc: int = 0,
        stderr: bytes = f"error near {CANARY_URL}".encode(),
    ) -> None:
        self.dump, self.dump_rc, self.toc, self.list_rc = dump, dump_rc, toc, list_rc
        self.read_rc = read_rc
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | int | None = None,
        timeout: float,
    ) -> "subprocess.CompletedProcess[bytes]":
        self.calls.append(list(argv))
        if "pg_dump" in argv:
            assert stdout is not None and not isinstance(stdout, int)
            stdout.write(self.dump)
            return subprocess.CompletedProcess(argv, self.dump_rc, None, self.stderr)
        assert stdin is not None
        assert stdin.read() == self.dump  # verification reads the file just written
        if "--list" in argv:
            return subprocess.CompletedProcess(argv, self.list_rc, self.toc, self.stderr)
        assert "--file=/dev/null" in argv  # the full read
        return subprocess.CompletedProcess(argv, self.read_rc, b"", self.stderr)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "backups"
    path.mkdir(mode=0o700)
    return path


def old_backup(folder: Path, days: int) -> Path:
    path = folder / f"aether-{(NOW - timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')}.dump"
    path.write_bytes(b"older good backup")
    path.chmod(0o600)
    return path


def test_successful_run_writes_a_private_verified_file(folder: Path) -> None:
    docker = FakeDocker()
    result = run_backup(folder, now=NOW, runner=docker)
    path = folder / "aether-20261116T033000Z.dump"
    assert result.file_name == path.name and result.size_bytes == len(docker.dump)
    assert path.read_bytes() == docker.dump
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [c[4:] for c in docker.calls][1:] == [
        ["pg_restore", "--list"],
        ["pg_restore", "--file=/dev/null"],
    ]
    assert result.table_data == 1
    assert not [p for p in folder.iterdir() if p.name.endswith(".partial")]


def test_dump_runs_in_the_container_without_any_secret() -> None:
    argv = dump_argv("aether-postgres-1", "aether", "aether")
    assert argv[:4] == ["docker", "exec", "-i", "aether-postgres-1"]
    assert "--no-password" in argv and "--format=custom" in argv
    assert not any("://" in a or "password=" in a.lower() for a in argv)


@pytest.mark.parametrize(
    ("docker", "stage"),
    [
        (FakeDocker(dump_rc=1), "dump"),
        (FakeDocker(dump=b""), "verify"),
        (FakeDocker(list_rc=1), "verify"),
        (FakeDocker(toc=b";\n; no data\n"), "verify"),
        (FakeDocker(read_rc=1), "verify"),  # cut off: the TOC reads, the data doesn't
    ],
)
def test_a_bad_dump_never_replaces_or_prunes_a_good_one(
    folder: Path, docker: FakeDocker, stage: str
) -> None:
    olds = [old_backup(folder, d) for d in range(1, 40)]
    with pytest.raises(BackupError) as info:
        run_backup(folder, now=NOW, runner=docker)
    assert info.value.stage == stage
    assert sorted(folder.glob("*.dump")) == sorted(olds)  # nothing added, nothing pruned
    assert not [p for p in folder.iterdir() if p.name.endswith(".partial")]


def test_a_good_run_prunes_and_keeps_the_new_one(folder: Path) -> None:
    for d in range(1, 40):
        old_backup(folder, d)
    result = run_backup(folder, now=NOW, runner=FakeDocker())
    names = {p.name for p in folder.glob("*.dump")}
    assert result.file_name in names
    assert result.pruned == 39 + 1 - len(names) and len(names) == result.kept


def test_partial_files_from_an_interrupted_run_are_removed(folder: Path) -> None:
    stale = folder / ".aether-20261115T033000Z.dump.partial"
    stale.write_bytes(b"half a dump")
    run_backup(folder, now=NOW, runner=FakeDocker())
    assert not stale.exists()


def test_two_runs_never_overlap(folder: Path) -> None:
    with core.backup.locked(folder), pytest.raises(BackupError) as info:
        run_backup(folder, now=NOW, runner=FakeDocker())
    assert info.value.stage == "lock"


def test_same_timestamp_never_overwrites(folder: Path) -> None:
    run_backup(folder, now=NOW, runner=FakeDocker(dump=b"first"))
    with pytest.raises(BackupError, match="already exists"):
        run_backup(folder, now=NOW, runner=FakeDocker(dump=b"second"))
    assert (folder / "aether-20261116T033000Z.dump").read_bytes() == b"first"


def test_docker_missing_is_a_dump_failure(folder: Path) -> None:
    def missing(*_: Any, **__: Any) -> "subprocess.CompletedProcess[bytes]":
        raise FileNotFoundError("docker")

    with pytest.raises(BackupError) as info:
        run_backup(folder, now=NOW, runner=missing)
    assert info.value.stage == "dump" and "FileNotFoundError" in str(info.value)


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o777])
def test_a_shared_folder_is_refused(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "shared"
    path.mkdir()
    path.chmod(mode)
    with pytest.raises(BackupError) as info:
        run_backup(path, now=NOW, runner=FakeDocker())
    assert info.value.stage == "folder"
    assert not list(path.iterdir())


def test_missing_folder_is_created_private(tmp_path: Path) -> None:
    path = tmp_path / "new" / "backups"
    run_backup(path, now=NOW, runner=FakeDocker())
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("kwargs", "kind"),
    [
        ({"container": "x; rm -rf /"}, "container"),
        ({"database": "Aether"}, "database"),
        ({"user": "aether --help"}, "user"),
    ],
)
def test_names_are_validated(folder: Path, kwargs: dict[str, Any], kind: str) -> None:
    with pytest.raises(BackupError, match=f"invalid {kind} name"):
        run_backup(folder, now=NOW, runner=FakeDocker(), **kwargs)


# --- the folder setting -------------------------------------------------------------


def test_default_folder_is_outside_the_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert backup_dir(settings()) == tmp_path / ".local/share/aether/backups"
    assert core.backup.REPO not in backup_dir(settings()).parents


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("relative/backups", "absolute path"),
        (str(core.backup.REPO / "backups"), "outside the repository"),
        (str(core.backup.REPO), "outside the repository"),
    ],
)
def test_bad_folder_setting_is_refused_without_echoing_it(value: str, error: str) -> None:
    with pytest.raises(BackupError) as info:
        backup_dir(settings(aether_backup_dir=value))
    assert error in str(info.value)
    assert value not in str(info.value)


# --- what a run logs (main) -----------------------------------------------------------


def run_main(
    monkeypatch: pytest.MonkeyPatch, folder: Path, docker: FakeDocker
) -> tuple[int, list[dict[str, Any]], str]:
    out = io.StringIO()
    s = settings(aether_backup_dir=str(folder), database_url=CANARY_URL)
    real = core.backup.run_backup

    def with_fake(*args: Any, **kwargs: Any) -> core.backup.Result:
        return real(*args, runner=docker, now=NOW, **kwargs)

    monkeypatch.setattr(core.backup, "run_backup", with_fake)
    monkeypatch.setattr(
        core.backup, "configure_logging", lambda st: configure_logging(st, stream=out)
    )
    code = core.backup.main([], settings=s)
    text = out.getvalue()
    return code, [json.loads(line) for line in text.splitlines()], text


def test_success_logs_one_metadata_line(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    code, records, text = run_main(monkeypatch, folder, FakeDocker())
    assert code == 0
    [record] = [r for r in records if r["logger"] == "aether.backup"]
    assert record["message"] == "Backup completed"
    assert record["backup_file"] == "aether-20261116T033000Z.dump"
    assert str(folder) not in text


@pytest.mark.parametrize("docker", [FakeDocker(dump_rc=2), FakeDocker(list_rc=1)])
def test_failure_exits_non_zero_with_one_safe_line(
    monkeypatch: pytest.MonkeyPatch, folder: Path, docker: FakeDocker
) -> None:
    code, records, text = run_main(monkeypatch, folder, docker)
    assert code == 1
    [record] = [r for r in records if r["logger"] == "aether.backup"]
    assert record["level"] == "ERROR" and record["message"].startswith("Backup failed")
    assert record["stage"] in {"dump", "verify"} and record["exit_code"] in {1, 2}
    # The tool's stderr (with a URL and password in it) is never logged, nor the folder.
    for secret in (CANARY_PASSWORD, CANARY_URL, "error near", str(folder)):
        assert secret not in text


def test_unexpected_error_logs_type_only(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    def boom(*_: Any, **__: Any) -> "subprocess.CompletedProcess[bytes]":
        raise RuntimeError(f"weird failure at {folder} with {CANARY_URL}")

    monkeypatch.setattr(core.backup, "prepare_dir", boom)
    code, records, text = run_main(monkeypatch, folder, FakeDocker())
    assert code == 1
    [record] = [r for r in records if r["logger"] == "aether.backup"]
    assert record["stage"] == "unexpected" and record["error_type"] == "RuntimeError"
    assert str(folder) not in text and CANARY_PASSWORD not in text
    assert "exc_info" not in record


def test_compose_names_match_the_default_container() -> None:
    import yaml

    compose = yaml.safe_load((core.backup.REPO / "docker-compose.yml").read_text())
    assert f"{compose['name']}-postgres-1" == core.backup.DEFAULT_CONTAINER
    assert compose["services"]["postgres"]["environment"]["POSTGRES_USER"] == "aether"


def test_backup_dir_is_documented_in_env_example() -> None:
    text = (core.backup.REPO / ".env.example").read_text()
    assert "\nAETHER_BACKUP_DIR=\n" in text


def test_os_umask_does_not_widen_files(folder: Path) -> None:
    old = os.umask(0)
    try:
        run_backup(folder, now=NOW, runner=FakeDocker())
    finally:
        os.umask(old)
    assert stat.S_IMODE((folder / "aether-20261116T033000Z.dump").stat().st_mode) == 0o600
    assert stat.S_IMODE((folder / ".lock").stat().st_mode) == 0o600


def test_logger_name_is_stable() -> None:
    assert core.backup.logger is logging.getLogger("aether.backup")


# --- the systemd templates (not installed by the repo) --------------------------------


def unit(name: str) -> str:
    return (core.backup.REPO / "scripts/systemd" / name).read_text()


def test_service_runs_the_backup_privately_without_secrets() -> None:
    service = unit("aether-backup.service")
    assert "ExecStart=/usr/bin/env uv run --no-sync python -m core.backup\n" in service
    assert "Type=oneshot" in service and "UMask=0077" in service
    assert "EnvironmentFile" not in service  # Settings reads .env itself; no copy here
    assert "://" not in service.replace("https://github.com/", "")


def test_timer_is_daily_and_catches_up() -> None:
    timer = unit("aether-backup.timer")
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_no_prune_keeps_every_backup(folder: Path) -> None:
    olds = [old_backup(folder, d) for d in range(0, 40)]
    result = run_backup(folder, now=NOW + timedelta(hours=1), runner=FakeDocker(), prune_old=False)
    assert result.pruned == 0
    assert set(olds) <= set(folder.glob("*.dump")) and result.kept == 41


# --- security review follow-ups -------------------------------------------------------


def test_labelled_backup_is_outside_retention(folder: Path) -> None:
    olds = [old_backup(folder, d) for d in range(0, 40)]
    result = run_backup(
        folder, now=NOW + timedelta(hours=1), runner=FakeDocker(), label="prerestore"
    )
    assert result.file_name == "aether-20261116T043000Z-prerestore.dump"
    assert result.pruned == 0 and set(olds) <= set(folder.glob("*.dump"))
    # Never matched by retention, so a later normal run neither keeps nor prunes it.
    run_backup(folder, now=NOW + timedelta(hours=2), runner=FakeDocker())
    assert (folder / result.file_name).exists()
    assert parse_backup(folder / result.file_name) is None


def test_bad_label_refused(folder: Path) -> None:
    with pytest.raises(BackupError, match="invalid label name"):
        run_backup(folder, now=NOW, runner=FakeDocker(), label="../x")


def test_the_backup_just_written_is_never_pruned(folder: Path) -> None:
    # The clock was once set ahead: 10 future-dated backups.
    for d in range(1, 11):
        p = folder / f"aether-{(NOW + timedelta(days=400 + d)).strftime('%Y%m%dT%H%M%SZ')}.dump"
        p.write_bytes(b"future")
    result = run_backup(folder, now=NOW, runner=FakeDocker())
    assert (folder / result.file_name).exists()


def test_a_much_smaller_backup_is_kept_and_warned(
    monkeypatch: pytest.MonkeyPatch, folder: Path
) -> None:
    old = old_backup(folder, 1)
    old.write_bytes(b"x" * 10_000)
    code, records, _ = run_main(monkeypatch, folder, FakeDocker(dump=b"tiny"))
    assert code == 0
    levels = [r["level"] for r in records if r["logger"] == "aether.backup"]
    assert levels == ["INFO", "WARNING"]


def test_folder_of_another_user_is_refused(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(BackupError, match="must belong to the user"):
        run_backup(folder, now=NOW, runner=FakeDocker())


def test_a_symlinked_lock_file_is_not_followed(folder: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    (folder / ".lock").symlink_to(target)
    with pytest.raises(OSError):
        run_backup(folder, now=NOW, runner=FakeDocker())
    assert not target.exists()


def test_a_bad_setting_logs_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> Settings:
        raise ValueError(f"bad value {CANARY_URL}")

    monkeypatch.setattr(core.backup, "get_settings", broken)
    assert core.backup.main([]) == 1
    out = capsys.readouterr().out
    [record] = [json.loads(line) for line in out.splitlines()]
    assert record["stage"] == "config" and record["error_type"] == "ValueError"
    assert CANARY_PASSWORD not in out and "Traceback" not in out


def test_invalid_database_name_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = settings(aether_backup_dir=str(folder))
    assert core.backup.main(["--database", "Evil Name; DROP"], settings=s) == 1
    out = capsys.readouterr().out
    assert "Evil Name" not in out and "invalid database name" in out
