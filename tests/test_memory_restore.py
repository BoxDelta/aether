"""``python -m memory.restore`` without a database (ADR-024 §4, step 16 of #58).
The live repair is tested in tests/test_memory_restore_live.py."""

import io
import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from core.config import Settings
from core.log import configure_logging
from memory.restore import _plan, database_of, main, owner_settings, with_database

URL = "postgresql+asyncpg://aether:pw-restore-unit-0123@127.0.0.1:5432/aether"  # gitleaks:allow


def settings() -> Settings:
    return Settings(_env_file=None, database_migration_url=SecretStr(URL))


def run_main(
    argv: list[str], s: Settings | None, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    out = io.StringIO()
    real = configure_logging

    def capture(settings: Settings, stream: Any = None) -> None:
        real(settings, stream=out)

    monkeypatch.setattr("memory.restore.configure_logging", capture)
    code = main(argv, settings=s)
    return code, out.getvalue()


@pytest.mark.parametrize(
    ("into", "why"),
    [
        ("aether", "live database"),
        ("x;drop", "plain database name"),
        ("Aether", "plain"),
        ("postgres", "cluster's own"),
        ("template1", "cluster's own"),
        ("template0", "cluster's own"),
    ],
)
def test_refuses_the_live_database_and_bad_names(
    into: str, why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, log = run_main(["--into", into, "--dry-run"], settings(), monkeypatch)

    assert code == 1
    record = json.loads(log.strip().splitlines()[-1])
    assert record["message"].startswith("Memory repair refused") and why in record["message"]
    assert "pw-restore-unit" not in log and "127.0.0.1" not in log


def test_without_a_url_it_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, log = run_main(["--into", "aether_restore_check"], Settings(_env_file=None), monkeypatch)

    assert code == 1 and "not set" in log


def test_url_helpers() -> None:
    assert database_of(settings()) == "aether"
    assert with_database(URL, "copy").endswith("/copy")
    other = owner_settings(settings(), "aether_restore_check")
    assert other.database_migration_url is not None
    assert other.database_migration_url.get_secret_value().endswith("/aether_restore_check")
    assert other.database_url is None


def test_an_exported_migration_url_cannot_redirect_the_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: BaseSettings reads the process environment even with
    # _env_file=None; an exported DATABASE_MIGRATION_URL must not win.
    monkeypatch.setenv("DATABASE_MIGRATION_URL", URL)
    other = owner_settings(settings(), "aether_restore_check")
    assert other.database_migration_url is not None
    assert other.database_migration_url.get_secret_value().endswith("/aether_restore_check")


def _event(kind: str, record_id: Any, level: str = "personal") -> dict[str, Any]:
    return {"event_type": kind, "memory_record_id": record_id, "memory_level": level}


def test_plan_deletes_gone_records_and_keeps_the_newest_level() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    saved = [
        _event("memory.updated", a, "personal"),
        _event("memory.updated", a, "sensitive"),
        _event("memory.approved", b, "public"),
        _event("memory.updated", c, "personal"),
        _event("memory.deleted", c),
        _event("memory.exported", None),
        {"event_type": "memory.context.retrieved", "memory_record_id": None, "memory_level": None},
    ]

    gone, newest = _plan(saved)

    assert gone == [c]
    assert newest == {a: "sensitive", b: "public"}


def test_plan_counts_a_record_once() -> None:
    a = uuid4()
    gone, newest = _plan([_event("memory.rejected", a), _event("memory.expired", a)])

    assert gone == [a] and newest == {}
