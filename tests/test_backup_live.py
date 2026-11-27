"""Backup and restore against a live Postgres (#3, ADR-025). Opt-in.

Never touches the real ``aether`` database. The test creates two temporary
databases in the cluster that ``DATABASE_URL`` points at, migrates and fills the
first, backs it up with ``core.backup`` (``pg_dump`` inside the container),
restores into the second the way the runbook does, and drops both. The container
is found by the port in ``DATABASE_URL``. Sentinel runs it against its throwaway
compose database.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import json
import secrets
import stat
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

import core.backup
from core.config import Settings
from core.db import check_database, create_engine, create_migration_engine
from tests.conftest import LIVE_DB

pytestmark = LIVE_DB

REPO = Path(__file__).resolve().parent.parent
INSUFFICIENT_PRIVILEGE = "42501"
EVENT = "INSERT INTO events (occurred_at, event_type, schema_version, actor) VALUES (now(), 'request.received', 1, 'core')"  # noqa: E501
RESTORE_EVENT = (
    "INSERT INTO events (occurred_at, event_type, schema_version, actor, memory_record_id,"
    " memory_category, memory_source, memory_level) VALUES (now(), 'memory.deleted', 1,"
    " 'restore', gen_random_uuid(), 'preference', 'owner', 'personal')"
)
CHECK_VIOLATION = "23514"
# Invented canary text only (migration 0004).
MEMORY = (
    "INSERT INTO memory_records (content, category, privacy, status, source, privacy_source,"
    " approved_at) VALUES ('canary-backup-3c9a: invented test fact', 'preference', 'personal',"
    " 'approved', 'owner', 'owner', now())"
)


def with_database(settings: Settings, database: str) -> Settings:
    """``settings`` with every URL pointing at ``database``."""

    def swap(url: SecretStr | None) -> SecretStr | None:
        if url is None:
            return None
        parts = urlsplit(url.get_secret_value())
        return SecretStr(urlunsplit(parts._replace(path=f"/{database}")))

    return Settings(
        _env_file=None,
        database_url=swap(settings.database_url),
        database_migration_url=swap(settings.database_migration_url),
    )


async def owner_autocommit(owner: Settings, statement: str) -> None:
    engine = create_migration_engine(owner)
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def owner_run(owner: Settings, body: Any) -> Any:
    engine = create_migration_engine(owner)
    try:
        async with engine.begin() as conn:
            return await body(conn)
    finally:
        await engine.dispose()


def owner_user(owner: Settings) -> str:
    assert owner.database_url is not None
    return urlsplit(owner.database_url.get_secret_value()).username or ""


def docker(
    argv: Sequence[str], *, stdin: IO[bytes] | None = None, data: bytes | None = None
) -> "subprocess.CompletedProcess[bytes]":
    """Run the docker CLI with a fixed argument list (never a shell)."""
    return subprocess.run(  # noqa: S603
        ["docker", *argv],  # noqa: S607
        stdin=stdin,
        input=data,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def container(live_owner: Settings) -> str:
    """The Postgres container that publishes DATABASE_URL's port."""
    assert live_owner.database_url is not None
    port = urlsplit(live_owner.database_url.get_secret_value()).port or 5432
    found = docker(["ps", "--filter", f"publish={port}", "--format", "{{.Names}}"])
    names = found.stdout.decode().split()
    if found.returncode != 0 or len(names) != 1:
        pytest.fail(f"expected one running container publishing port {port}, found {names}")
    return names[0]


@pytest.fixture
def databases(live_owner: Settings) -> Iterator[tuple[str, str]]:
    suffix = secrets.token_hex(5)
    src, dst = f"aether_bkp_src_{suffix}", f"aether_bkp_dst_{suffix}"
    try:
        for name in (src, dst):
            asyncio.run(owner_autocommit(live_owner, f"CREATE DATABASE {name}"))
        yield src, dst
    finally:
        for name in (src, dst):
            asyncio.run(
                owner_autocommit(live_owner, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            )


def migrate(owner: Settings) -> None:
    config = Config(str(REPO / "alembic.ini"))

    def upgrade(sync: Connection) -> None:
        config.attributes["connection"] = sync
        command.upgrade(config, "head")

    async def body(conn: AsyncConnection) -> None:
        await conn.run_sync(upgrade)

    asyncio.run(owner_run(owner, body))


def add_events(owner: Settings, n: int, statement: str = EVENT) -> None:
    async def body(conn: AsyncConnection) -> None:
        for _ in range(n):
            await conn.execute(text(statement))

    asyncio.run(owner_run(owner, body))


def counts(owner: Settings, upto: int | None = None) -> dict[str, Any]:
    async def body(conn: AsyncConnection) -> dict[str, Any]:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*), max(id) FROM events"
                    " WHERE CAST(:upto AS bigint) IS NULL OR id <= :upto"
                ),
                {"upto": upto},
            )
        ).one()
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        memory = await conn.scalar(text("SELECT count(*) FROM memory_records"))
        return {"events": row[0], "max_id": row[1], "alembic": version, "memory": memory}

    result: dict[str, Any] = asyncio.run(owner_run(owner, body))
    return result


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def back_up(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    folder: Path,
    container: str,
    database: str,
    user: str,
    app: Settings,
) -> tuple[Path, str]:
    """Run ``core.backup.main`` for real. Returns the dump and everything shown:
    stdout, stderr and every process argument list it started."""
    started: list[list[str]] = []
    real_run = subprocess.run

    def watching(argv: Sequence[str], **kwargs: Any) -> Any:
        started.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", watching)  # core.backup calls subprocess.run
    settings = Settings(
        _env_file=None,
        aether_backup_dir=str(folder),
        database_url=app.database_url,
        database_migration_url=app.database_migration_url,
    )
    code = core.backup.main(
        ["--container", container, "--database", database, "--user", user], settings=settings
    )
    printed = capsys.readouterr()
    assert code == 0, printed.out
    assert [argv[4:6] for argv in started] == [
        ["pg_dump", "--format=custom"],
        ["pg_restore", "--list"],
        ["pg_restore", "--file=/dev/null"],
    ]
    [dump] = list(folder.glob(f"{database}-*.dump"))
    record = json.loads(printed.out.strip().splitlines()[-1])
    assert record["message"] == "Backup completed" and record["backup_file"] == dump.name
    return dump, printed.out + printed.err + json.dumps(started)


def restore(container: str, user: str, database: str, dump: Path) -> None:
    """As the runbook (step R2): into an empty database, all or nothing."""
    with dump.open("rb") as archive:
        done = docker(
            [
                "exec",
                "-i",
                container,
                "pg_restore",
                "--no-password",
                f"--username={user}",
                f"--dbname={database}",
                "--exit-on-error",
                "--single-transaction",
            ],
            stdin=archive,
        )
    assert done.returncode == 0, "pg_restore failed (stderr not shown: may name objects)"


def restore_check(container: str, user: str, database: str) -> str:
    """The runbook's check (scripts/restore_check.sql), as the owner."""
    done = docker(
        [
            "exec",
            "-i",
            container,
            "psql",
            "--no-password",
            "-At",
            f"--username={user}",
            f"--dbname={database}",
        ],
        data=(REPO / "scripts/restore_check.sql").read_bytes(),
    )
    return done.stdout.decode().strip()


def secrets_of(*settings: Settings) -> list[str]:
    found = []
    for s in settings:
        for url in (s.database_url, s.database_migration_url):
            if url is not None:
                found += [
                    url.get_secret_value(),
                    unquote(urlsplit(url.get_secret_value()).password or ""),
                ]
    return [f for f in found if f]


def owner_insert(statement: str) -> Any:
    """A body for owner_run: run one statement, rolled back by a savepoint."""

    async def body(conn: AsyncConnection) -> str:
        nested = await conn.begin_nested()
        try:
            await conn.execute(text(statement))
        except DBAPIError as exc:
            return sqlstate(exc) or type(exc.orig).__name__
        finally:
            await nested.rollback()
        return "allowed"

    return body


async def as_app(app: Settings, statement: str) -> str:
    """Run one statement as Core's app role, always rolled back."""
    engine = create_engine(app)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(statement))
            except DBAPIError as exc:
                return sqlstate(exc) or type(exc.orig).__name__
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
    return "allowed"


async def owner_update(conn: AsyncConnection) -> str:
    try:
        await conn.execute(text("UPDATE events SET outcome = 'ok'"))
    except DBAPIError as exc:
        return str(exc.orig)
    return "allowed"


def test_backup_restore_round_trip_keeps_rows_triggers_and_grants(
    live_owner: Settings,
    live_app: Settings,
    container: str,
    databases: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src, dst = databases
    user = owner_user(live_owner)
    src_owner, dst_owner = with_database(live_owner, src), with_database(live_owner, dst)
    migrate(src_owner)
    add_events(src_owner, 5)
    add_events(src_owner, 3, MEMORY)

    folder = tmp_path / "backups"
    dump, shown = back_up(
        monkeypatch, capsys, folder=folder, container=container, database=src, user=user,
        app=live_app,
    )  # fmt: skip
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    # Canary: no password, URL or folder in any output, log line or process argument.
    for secret in [*secrets_of(live_owner, live_app), str(folder)]:
        assert secret not in shown

    add_events(src_owner, 5)  # written after the backup: must not come back
    add_events(src_owner, 2, MEMORY)
    restore(container, user, dst, dump)

    after = counts(dst_owner)
    assert after["events"] == 5 and after["memory"] == 3
    assert {**after, "memory": 5} == counts(src_owner, upto=after["max_id"])
    assert counts(src_owner)["events"] == 10

    # Database-level rights are not in a dump: the runbook sets them (step R3).
    assert restore_check(container, user, dst) == "NOT OK: public_database"
    asyncio.run(owner_autocommit(live_owner, f"REVOKE ALL ON DATABASE {dst} FROM PUBLIC"))
    asyncio.run(owner_autocommit(live_owner, f"GRANT CONNECT ON DATABASE {dst} TO aether_app"))
    assert restore_check(container, user, dst) == "ok"

    # Triggers: the owner hits them; the app role is refused before them.
    assert "events is append-only" in asyncio.run(owner_run(dst_owner, owner_update))
    app_dst = with_database(live_app, dst)
    assert asyncio.run(check_database(app_dst)).ok  # Core's privilege check passes
    assert asyncio.run(as_app(app_dst, EVENT)) == "allowed"  # rolled back
    # 0007 (#150): actor 'restore' is the owner's only, also in the restored copy.
    assert asyncio.run(as_app(app_dst, RESTORE_EVENT)) == CHECK_VIOLATION
    assert asyncio.run(owner_run(dst_owner, owner_insert(RESTORE_EVENT))) == "allowed"
    for statement in ("UPDATE events SET outcome = 'ok'", "DELETE FROM events", "TRUNCATE events"):
        assert asyncio.run(as_app(app_dst, statement)) == INSUFFICIENT_PRIVILEGE, statement
    # memory_records (0004): the grants came back with the table, and no more.
    for statement in (
        MEMORY,
        "SELECT count(*) FROM memory_records",
        "UPDATE memory_records SET privacy = 'sensitive'",
        "DELETE FROM memory_records",
    ):
        assert asyncio.run(as_app(app_dst, statement)) == "allowed", statement  # rolled back
    assert asyncio.run(as_app(app_dst, "TRUNCATE memory_records")) == INSUFFICIENT_PRIVILEGE


def test_restore_check_flags_a_database_from_before_0004(
    live_owner: Settings, container: str, databases: tuple[str, str]
) -> None:
    # A backup from before migration 0004 restores without memory_records: the check
    # names only that (runbook R4), instead of failing with an SQL error.
    src, _ = databases
    owner = with_database(live_owner, src)
    config = Config(str(REPO / "alembic.ini"))

    def upgrade(sync: Connection) -> None:
        config.attributes["connection"] = sync
        command.upgrade(config, "0003")

    async def body(conn: AsyncConnection) -> None:
        await conn.run_sync(upgrade)

    asyncio.run(owner_run(owner, body))
    assert restore_check(container, owner_user(live_owner), src) == "NOT OK: memory_table"


def test_restore_check_flags_a_missing_restore_actor_trigger(
    live_owner: Settings, container: str, databases: tuple[str, str]
) -> None:
    # A copy at 0007 or later must have the trigger that keeps actor 'restore' for
    # the owner (#150): the check names it if a restore lost it.
    src, _ = databases
    owner = with_database(live_owner, src)
    migrate(owner)
    assert restore_check(container, owner_user(live_owner), src) == "ok"

    async def drop(conn: AsyncConnection) -> None:
        await conn.execute(text("DROP TRIGGER events_restore_actor_owner_only ON events"))

    asyncio.run(owner_run(owner, drop))
    assert restore_check(container, owner_user(live_owner), src) == "NOT OK: restore_actor_trigger"
