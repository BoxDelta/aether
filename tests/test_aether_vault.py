"""scripts/aether-vault.fish (ADR-026, #57): fails closed, holds no secret.

Every host tool is a stub that only records its arguments (sudo, cryptsetup,
mount, umount, mountpoint, findmnt, docker), so nothing here touches a real
device, mount or container. State (unlocked, mounted) lives in files the stubs
read and write.
"""

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "aether-vault.fish"
FISH = shutil.which("fish")
MAPPER = "/dev/mapper/aether-vault"

pytestmark = pytest.mark.skipif(FISH is None, reason="fish not installed")

# Each stub logs "tool args" and acts on $STUB_STATE (a folder of flag files).
STUBS = {
    # sudo runs the rest of the line (the stubs below), as the real one would.
    "sudo": 'echo "sudo $*" >> "$STUB_LOG"; exec "$@"',
    "cryptsetup": """echo "cryptsetup $*" >> "$STUB_LOG"
case "$1" in
  status) [ -f "$STUB_STATE/unlocked" ] ;;
  open) [ -z "$STUB_OPEN_FAIL" ] || exit 2; touch "$STUB_STATE/unlocked" ;;
  close) rm -f "$STUB_STATE/unlocked" ;;
  *) exit 9 ;;
esac""",
    "mount": """echo "mount $*" >> "$STUB_LOG"
[ -z "$STUB_MOUNT_FAIL" ] || exit 32
touch "$STUB_STATE/mounted"
[ -n "$STUB_NO_MARKERS" ] || { mkdir -p "$STUB_MNT/data" "$STUB_MNT/backups"
  touch "$STUB_MNT/.aether-vault" "$STUB_MNT/data/.aether-vault"; }""",
    "umount": """echo "umount $*" >> "$STUB_LOG"
[ -z "$STUB_UMOUNT_FAIL" ] || exit 32
rm -f "$STUB_STATE/mounted"; rm -rf "$STUB_MNT"/* "$STUB_MNT"/.aether-vault""",
    "mountpoint": '[ -f "$STUB_STATE/mounted" ]',
    "findmnt": 'echo "${STUB_SOURCE:-/dev/mapper/aether-vault}"',
    "docker": """echo "docker $*" >> "$STUB_LOG"
[ -z "$STUB_DOCKER_FAIL" ] || exit 1""",
}


@dataclass
class Result:
    code: int
    out: str
    calls: list[str]

    def called(self, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in self.calls)


def run(
    tmp_path: Path, *args: str, container_file: bool = True, mounted: bool = False, **flags: str
) -> Result:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    share = home / ".local/share/aether"
    mnt = share / "vault"
    mnt.mkdir(parents=True, exist_ok=True)
    repo = home / "Projects/aether"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "compose.vault.yaml").touch()
    if container_file:
        (share / "store").mkdir(exist_ok=True)
        (share / "store/aether-vault.luks").touch()
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if mounted:
        (state / "unlocked").touch()
        (state / "mounted").touch()
        (mnt / "data").mkdir(exist_ok=True)
        (mnt / "backups").mkdir(exist_ok=True)
        (mnt / ".aether-vault").touch()
        (mnt / "data/.aether-vault").touch()
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "STUB_LOG": str(log),
        "STUB_STATE": str(state),
        "STUB_MNT": str(mnt),
        **{f"STUB_{k.upper()}": v for k, v in flags.items()},
    }
    quoted = " ".join(f"'{a}'" for a in args)
    proc = subprocess.run(  # noqa: S603 - fixed argv, stubbed tools
        [FISH or "fish", "--no-config", "-c", f"source {SCRIPT}; aether-vault {quoted}"],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return Result(proc.returncode, proc.stdout + proc.stderr, log.read_text().splitlines())


def started_postgres(result: Result) -> bool:
    return any(c.startswith("docker compose") and " up " in c for c in result.calls)


# --- open: the happy path ------------------------------------------------------------


def test_open_unlocks_mounts_checks_then_starts_postgres(tmp_path: Path) -> None:
    r = run(tmp_path, "open")
    assert r.code == 0, r.out
    steps = [c.split()[0] + " " + c.split()[1] for c in r.calls if not c.startswith("sudo")]
    assert steps == [
        "cryptsetup status",
        "cryptsetup open",
        "mount -o",
        "docker compose",
    ]
    luks = tmp_path / "home/.local/share/aether/store/aether-vault.luks"
    assert f"cryptsetup open --type luks2 {luks} aether-vault" in r.calls
    assert (
        f"mount -o nodev,nosuid,noexec {MAPPER} {tmp_path}/home/.local/share/aether/vault"
        in r.calls
    )
    up = next(c for c in r.calls if c.startswith("docker"))
    assert "-f " + str(tmp_path / "home/Projects/aether/docker-compose.yml") in up
    assert "-f " + str(tmp_path / "home/Projects/aether/compose.vault.yaml") in up
    assert up.endswith("up -d --wait postgres")


def test_open_when_already_mounted_only_checks_and_starts(tmp_path: Path) -> None:
    r = run(tmp_path, "open", mounted=True)
    assert r.code == 0, r.out
    assert not r.called("cryptsetup open") and not r.called("mount")
    assert started_postgres(r)


# --- open: every failure stops before Postgres ----------------------------------------


def test_no_container_file_touches_nothing(tmp_path: Path) -> None:
    r = run(tmp_path, "open", container_file=False)
    assert r.code == 1 and "no container file" in r.out
    assert r.calls == []


def test_wrong_passphrase_mounts_nothing_and_starts_nothing(tmp_path: Path) -> None:
    r = run(tmp_path, "open", open_fail="1")
    assert r.code == 1 and "unlock failed" in r.out
    assert not r.called("mount") and not started_postgres(r)


def test_mount_failure_locks_again_and_starts_nothing(tmp_path: Path) -> None:
    r = run(tmp_path, "open", mount_fail="1")
    assert r.code == 1 and "mount failed" in r.out
    assert r.called("cryptsetup close aether-vault")
    assert not started_postgres(r)


def test_missing_markers_start_nothing(tmp_path: Path) -> None:
    r = run(tmp_path, "open", no_markers="1")
    assert r.code == 1 and "marker" in r.out
    assert not started_postgres(r)


def test_something_else_mounted_there_starts_nothing(tmp_path: Path) -> None:
    r = run(tmp_path, "open", mounted=True, source="/dev/nvme0n1p2")
    assert r.code == 1 and "mounted from something else" in r.out
    assert not started_postgres(r)


def test_postgres_not_healthy_is_a_failure(tmp_path: Path) -> None:
    r = run(tmp_path, "open", docker_fail="1")
    assert r.code == 1 and "did not become healthy" in r.out


# --- close -----------------------------------------------------------------------------


def test_close_stops_postgres_then_unmounts_then_locks(tmp_path: Path) -> None:
    r = run(tmp_path, "close", mounted=True)
    assert r.code == 0, r.out
    order = [
        c for c in r.calls if not c.startswith("sudo") and not c.startswith("cryptsetup status")
    ]
    assert order[0].startswith("docker compose") and order[0].endswith("stop postgres")
    assert order[1].startswith("umount ")
    assert order[2] == "cryptsetup close aether-vault"


def test_close_never_unmounts_under_a_running_database(tmp_path: Path) -> None:
    r = run(tmp_path, "close", mounted=True, docker_fail="1")
    assert r.code == 1 and "could not stop Postgres" in r.out
    assert not r.called("umount") and not r.called("cryptsetup close")


def test_close_keeps_it_unlocked_if_unmount_fails(tmp_path: Path) -> None:
    r = run(tmp_path, "close", mounted=True, umount_fail="1")
    assert r.code == 1 and "unmount failed" in r.out
    assert not r.called("cryptsetup close")


def test_close_when_already_locked_only_stops_postgres(tmp_path: Path) -> None:
    r = run(tmp_path, "close")
    assert r.code == 0, r.out
    assert not r.called("umount") and not r.called("cryptsetup close")


def test_status_reports_without_changing_anything(tmp_path: Path) -> None:
    r = run(tmp_path, "status", mounted=True)
    assert "container file: present" in r.out and "unlocked: yes" in r.out
    assert "mounted: yes, markers ok" in r.out
    assert not r.called("cryptsetup open") and not r.called("mount")
    assert not r.called("umount") and not r.called("cryptsetup close")
    assert not any(" up " in c or " stop " in c for c in r.calls)
    (tmp_path / "locked").mkdir()
    locked = run(tmp_path / "locked", "status")
    assert "unlocked: no" in locked.out and "mounted: no" in locked.out


def test_unknown_command_prints_usage(tmp_path: Path) -> None:
    r = run(tmp_path, "format")
    assert r.code == 1 and "usage: aether-vault open | close | status" in r.out
    assert r.calls == []


# --- no secrets, nothing destructive ------------------------------------------------


def test_script_holds_no_secret_and_never_formats_or_deletes() -> None:
    text = SCRIPT.read_text()
    for forbidden in (
        "--key-file",
        "luksFormat",
        "luksAddKey",
        "mkfs",
        "rm ",
        "down",
        "dd ",
        "passphrase=",
        "PASSWORD",
        ".env",
    ):
        assert forbidden not in text, forbidden
    # The passphrase is only ever typed at cryptsetup's own prompt.
    assert re.search(r"cryptsetup open --type luks2 \$file \$name\n", text)


def test_fish_syntax_and_autoload_name() -> None:
    proc = subprocess.run(  # noqa: S603
        [FISH or "fish", "--no-execute", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    # Autoload: ~/.config/fish/functions/aether-vault.fish must define aether-vault.
    assert SCRIPT.read_text().startswith("function aether-vault ")
