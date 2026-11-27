"""The encrypted-volume opt-ins (ADR-026, #57): compose override and backup guard.

Nothing here touches a real mount, device or container. The backup guard's
ExecStartPre line is run with a stubbed ``mountpoint`` against temporary folders.
How the override behaves under Docker (missing source refused, no marker refused,
marker present starts) was checked by hand on a throwaway project; see the PR.
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
OVERRIDE = REPO / "compose.vault.yaml"
DROPIN = REPO / "scripts/systemd/aether-backup.service.d/vault.conf"
TARGET = "/var/lib/postgresql"


def override_service() -> dict[str, object]:
    service: dict[str, object] = yaml.safe_load(OVERRIDE.read_text())["services"]["postgres"]
    return service


# --- compose.vault.yaml -----------------------------------------------------------------


def test_data_is_a_bind_mount_that_docker_never_creates() -> None:
    volumes = override_service()["volumes"]
    assert volumes == [
        {
            "type": "bind",
            "source": "${HOME:?HOME must be set}/.local/share/aether/vault/data",
            "target": TARGET,
            "bind": {"create_host_path": False},
        }
    ]


def test_override_replaces_the_default_volume_target() -> None:
    # Compose merges volumes by target: the same target replaces the named volume.
    # Any other target would leave the image's VOLUME to an anonymous volume on
    # the unencrypted disk.
    base = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["postgres"]
    assert base["volumes"] == [f"pgdata:{TARGET}"]  # the default stays as it is (opt-in)


def test_postgres_is_not_started_at_boot_and_checks_the_marker() -> None:
    service = override_service()
    assert service["restart"] == "no"
    entrypoint = service["entrypoint"]
    assert isinstance(entrypoint, list) and entrypoint[:2] == ["/bin/sh", "-c"]
    script = entrypoint[2]
    assert f"[ ! -f {TARGET}/.aether-vault ]" in script and "exit 1" in script
    assert script.rstrip().endswith('exec docker-entrypoint.sh "$$@"')
    command = service["command"]
    assert isinstance(command, list) and command[0] == "postgres"
    # Errors must not quote rows (memory content) into the unencrypted Docker log.
    assert "log_error_verbosity=terse" in command
    assert "log_min_error_statement=panic" in command


def test_marker_check_refuses_without_the_marker(tmp_path: Path) -> None:
    # The entrypoint script itself, with compose's $$ unescaped and the path moved.
    script = override_service()["entrypoint"][2].replace("$$", "$")  # type: ignore[index]
    script = script.replace(TARGET, str(tmp_path)).replace("docker-entrypoint.sh", "echo started")

    def start() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", script, "docker-entrypoint.sh", "postgres"],
            capture_output=True,
            text=True,
            check=False,
        )

    refused = start()
    assert refused.returncode == 1 and "marker missing" in refused.stderr
    assert "started" not in refused.stdout
    (tmp_path / ".aether-vault").touch()
    ok = start()
    assert ok.returncode == 0 and ok.stdout.strip().startswith("started postgres")


def test_psql_history_stays_on_the_volume() -> None:
    # `docker compose exec postgres psql` runs as root (HOME=/root, in the
    # container's layer on the unencrypted disk) and inherits the service env.
    assert override_service()["environment"] == {"PSQL_HISTORY": f"{TARGET}/.psql_history"}


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_merged_config_has_exactly_one_bind_mount_and_no_named_volume(tmp_path: Path) -> None:
    # compose's own merge, offline (`config` needs no daemon). A throwaway env file
    # replaces .env, which is never read; the output is parsed, never printed.
    env_file = tmp_path / "env"
    env_file.write_text("POSTGRES_PASSWORD=throwaway-not-a-secret\n")
    home = tmp_path / "home"
    docker = shutil.which("docker") or "docker"
    argv = [docker, "compose", "--env-file", str(env_file), "-f", str(REPO / "docker-compose.yml")]
    argv += ["-f", str(OVERRIDE), "config", "--format", "json"]
    proc = subprocess.run(  # noqa: S603 - fixed argv
        argv,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 and "is not a docker command" in proc.stderr:
        pytest.skip("docker compose plugin not installed")
    assert proc.returncode == 0, "docker compose config failed"
    merged = json.loads(proc.stdout)
    service = merged["services"]["postgres"]
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": f"{home}/.local/share/aether/vault/data",
            "target": TARGET,
            "bind": {"create_host_path": False},
        }
    ]
    assert service["restart"] == "no"
    assert not merged.get("volumes")  # the named volume is gone from the merge


def test_override_holds_no_secret() -> None:
    text = OVERRIDE.read_text()
    assert "PASSWORD" not in text and "://" not in text and "passphrase:" not in text


# --- the backup drop-in --------------------------------------------------------------


def dropin_lines(prefix: str) -> list[str]:
    return [line for line in DROPIN.read_text().splitlines() if line.startswith(prefix)]


def test_backups_move_onto_the_volume() -> None:
    assert dropin_lines("Environment=") == [
        "Environment=AETHER_BACKUP_DIR=%h/.local/share/aether/vault/backups"
    ]
    assert "EnvironmentFile" not in DROPIN.read_text()


def test_backup_has_the_volume_guard() -> None:
    (guard,) = dropin_lines("ExecStartPre=")
    assert "mountpoint -q" in guard and ".aether-vault" in guard
    assert guard.endswith(" aether-vault-guard %h/.local/share/aether/vault")


def _run_guard(tmp_path: Path, *, mounted: bool) -> subprocess.CompletedProcess[str]:
    """The ExecStartPre line as systemd would run it: %h is the home folder and
    $$ is a literal $."""
    (guard,) = dropin_lines("ExecStartPre=")
    home = tmp_path / "home"
    command = guard.removeprefix("ExecStartPre=").replace("%h", str(home)).replace("$$", "$")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "mountpoint"
    stub.write_text(f"#!/bin/sh\nexit {0 if mounted else 1}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return subprocess.run(  # noqa: S603 - the unit's own line, stubbed mountpoint
        shlex.split(command),
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("mounted", [False, True])
def test_guard_fails_loudly_without_the_volume(tmp_path: Path, mounted: bool) -> None:
    # Not mounted, or mounted but without the marker and backups folder.
    (tmp_path / "home/.local/share/aether/vault").mkdir(parents=True)
    result = _run_guard(tmp_path, mounted=mounted)
    assert result.returncode == 1
    assert result.stderr.startswith("<3>aether-backup: encrypted volume not mounted")
    assert not (tmp_path / "home/.local/share/aether/vault/backups").exists()


def test_guard_passes_on_the_mounted_volume(tmp_path: Path) -> None:
    vault = tmp_path / "home/.local/share/aether/vault"
    (vault / "backups").mkdir(parents=True)
    (vault / ".aether-vault").touch()
    result = _run_guard(tmp_path, mounted=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_main_service_template_is_unchanged_by_the_opt_in() -> None:
    # The drop-in is opt-in: the template alone still backs up to the default folder.
    service = (REPO / "scripts/systemd/aether-backup.service").read_text()
    assert "vault" not in service and "AETHER_BACKUP_DIR" not in service
