"""Phase 2 exit 3 against a live Postgres: deletion and restore (ADR-024 §4, §8;
step 16 of #58; #67). Opt-in.

Never touches the real ``aether`` database. Like tests/test_backup_live.py it
creates two temporary databases: it fills the first through the real store,
backs it up with ``core.backup`` (``pg_dump`` in the container), changes memory
after the backup (delete, reject, raise a level, correct, create), restores into
the second the way the runbook does, runs ``memory.restore`` and checks the
result. All content is invented canaries.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from core.config import Settings
from core.db import create_migration_engine
from core.log import SecretCheck
from memory.restore import RepairRefused, run
from memory.store import PENDING_MAX, Approval, MemoryStore, NewRecord, PendingLimit, RecordChanges
from tests.conftest import LIVE_DB
from tests.test_backup_live import (
    back_up,
    container,  # noqa: F401 - fixture
    databases,  # noqa: F401 - fixture
    migrate,
    owner_autocommit,
    owner_user,
    restore,
    with_database,
)

pytestmark = LIVE_DB

CANARY = "canary-restorelive-6f1b"


def on[T](owner: Settings, body: Callable[[MemoryStore], Awaitable[T]]) -> T:
    """``body(store)`` with a store on ``owner``'s database (owner engine)."""

    async def go() -> T:
        engine = create_migration_engine(owner)
        try:
            return await body(MemoryStore(engine, SecretCheck()))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def sql(owner: Settings, statement: str, **params: Any) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_migration_engine(owner)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(statement), params)
                return list(result) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(go())


def owner_record(name: str, **kw: Any) -> NewRecord:
    values: dict[str, Any] = {
        "content": f"{CANARY} {name}",
        "category": "project",
        "privacy": "personal",
        "source": "owner",
    }
    return NewRecord(**{**values, **kw})


def proposal(name: str) -> NewRecord:
    return NewRecord(
        content=f"{CANARY} {name}",
        category="project",
        privacy="personal",
        source="model",
        source_provider="ollama",
        source_request_id="req-restorelive.1",
    )


def records(owner: Settings) -> dict[str, dict[str, Any]]:
    rows = sql(
        owner,
        "SELECT id, content, privacy, status, restored, approved_at FROM memory_records",
    )
    return {r.content.removeprefix(f"{CANARY} "): dict(r._mapping) for r in rows}


def memory_events(owner: Settings) -> list[dict[str, Any]]:
    rows = sql(
        owner,
        "SELECT row_to_json(e)::text AS j FROM events e"
        " WHERE event_type LIKE 'memory.%' ORDER BY id",
    )
    return [json.loads(r.j) for r in rows]


@pytest.fixture
def scenario(
    live_owner: Settings,
    live_app: Settings,
    container: str,  # noqa: F811
    databases: tuple[str, str],  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    src, dst = databases
    user = owner_user(live_owner)
    src_owner, dst_owner = with_database(live_owner, src), with_database(live_owner, dst)
    migrate(src_owner)

    async def before(s: MemoryStore) -> dict[str, UUID]:
        ids = {
            "deleted": (await s.create(owner_record("deleted"))).id,
            "raised": (await s.create(owner_record("raised"))).id,
            "corrected": (await s.create(owner_record("corrected", privacy="public"))).id,
            "kept": (await s.create(owner_record("kept"))).id,
            "rejected": (await s.create(proposal("rejected"))).id,
            "approved-later": (await s.create(proposal("approved-later"))).id,
        }
        return ids

    ids = on(src_owner, before)
    # Old enough that, without the restored marker, the copy's clean-up would
    # delete it at once once it is pending again (#67).
    sql(
        src_owner,
        "UPDATE memory_records SET created_at = :old WHERE id = :id",
        old=datetime.now(UTC) - timedelta(days=40),
        id=ids["corrected"],
    )

    dump, _ = back_up(
        monkeypatch, capsys, folder=tmp_path / "b", container=container, database=src,
        user=user, app=live_app,
    )  # fmt: skip

    async def after(s: MemoryStore) -> None:
        await s.delete(ids["deleted"], actor="owner")
        await s.update(ids["raised"], RecordChanges(privacy="sensitive"), actor="owner")
        await s.update(
            ids["corrected"], RecordChanges(content=f"{CANARY} corrected NEW"), actor="owner"
        )
        await s.reject(ids["rejected"], actor="owner")
        await s.approve(ids["approved-later"], Approval("public"), actor="owner")
        await s.create(owner_record("created-later"))

    on(src_owner, after)
    restore(container, user, dst, dump)
    for statement in (
        f"REVOKE ALL ON DATABASE {dst} FROM PUBLIC",
        f"GRANT CONNECT ON DATABASE {dst} TO aether_app",
    ):
        asyncio.run(owner_autocommit(live_owner, statement))
    return {"src": src_owner, "src_name": src, "dst": dst_owner, "dst_name": dst, "ids": ids}


def test_restore_brings_back_the_past_until_repaired(scenario: dict[str, Any]) -> None:
    restored = records(scenario["dst"])

    # Row counts: the copy is the backup, not the live database.
    assert set(restored) == {
        "deleted", "raised", "corrected", "kept", "rejected", "approved-later",
    }  # fmt: skip
    assert set(records(scenario["src"])) == {
        "raised", "corrected NEW", "kept", "approved-later", "created-later",
    }  # fmt: skip
    assert restored["raised"]["privacy"] == "personal"  # the raise is undone


def test_repair_deletes_again_raises_levels_and_marks_restored(scenario: dict[str, Any]) -> None:
    src, dst = scenario["src"], scenario["dst"]
    saved_after = [e for e in memory_events(src) if e["id"] > max_event_id(dst)]

    result = asyncio.run(run(src, scenario["dst_name"], dry_run=False))

    now = records(dst)
    assert set(now) == {"raised", "corrected", "kept", "approved-later"}
    # Deleted and rejected records are gone again.
    assert result.deleted_again == 2
    # Raised: pending, restored, at the higher level; approved_at cleared.
    raised = now["raised"]
    assert (raised["status"], raised["restored"], raised["privacy"]) == (
        "pending",
        True,
        "sensitive",
    )
    assert raised["approved_at"] is None
    # Corrected: the old text came back, so it is pending for the owner to re-check.
    assert (now["corrected"]["status"], now["corrected"]["restored"]) == ("pending", True)
    assert (now["approved-later"]["status"], now["approved-later"]["restored"]) == (
        "pending",
        True,
    )
    assert (now["kept"]["status"], now["kept"]["restored"]) == ("approved", False)
    assert (result.set_pending, result.restored_total) == (3, 3)
    assert result.events_copied == len(saved_after)

    events = memory_events(dst)
    copied = [(e["event_type"], e["memory_record_id"], e["occurred_at"]) for e in saved_after]
    assert all(
        (e["event_type"], e["memory_record_id"], e["occurred_at"]) in
        [(c["event_type"], c["memory_record_id"], c["occurred_at"]) for c in events]
        for e in saved_after
    ) and copied  # fmt: skip
    by_restore = [
        (e["event_type"], e["memory_record_id"]) for e in events if e["actor"] == "restore"
    ]
    ids = scenario["ids"]
    assert sorted(by_restore) == sorted(
        [
            ("memory.deleted", str(ids["deleted"])),
            ("memory.deleted", str(ids["rejected"])),
            ("memory.updated", str(ids["raised"])),
            ("memory.updated", str(ids["corrected"])),
            ("memory.updated", str(ids["approved-later"])),
        ]
    )
    raised_event = next(
        e for e in events if e["actor"] == "restore" and e["memory_record_id"] == str(ids["raised"])
    )
    assert (raised_event["memory_level"], raised_event["memory_old_level"]) == (
        "sensitive",
        "personal",
    )
    # No event column holds content.
    assert CANARY not in json.dumps(events)


def test_restored_records_survive_the_clean_up_and_the_pending_limit(
    scenario: dict[str, Any],
) -> None:
    dst = scenario["dst"]
    asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=False))

    async def body(s: MemoryStore) -> tuple[int, int, str]:
        expired = await s.expire()
        made = 0
        for i in range(PENDING_MAX):  # restored records don't count toward the limit
            await s.create(proposal(f"limit {i}"))
            made += 1
        try:
            await s.create(proposal("one too many"))
        except PendingLimit:
            return expired, made, "refused"
        return expired, made, "accepted"

    expired, made, extra = on(dst, body)

    assert expired == 0  # "corrected" is 40 days old but restored
    assert "corrected" in records(dst)
    assert (made, extra) == (PENDING_MAX, "refused")


def test_approving_a_restored_record_clears_the_marker(scenario: dict[str, Any]) -> None:
    dst = scenario["dst"]
    asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=False))
    raised = scenario["ids"]["raised"]

    record = on(dst, lambda s: s.approve(raised, Approval("sensitive"), actor="owner"))

    assert (record.status, record.restored) == ("approved", False)


def test_repair_refuses_to_run_twice(scenario: dict[str, Any]) -> None:
    asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=False))

    with pytest.raises(RepairRefused, match="already repaired"):
        asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=False))


def test_dry_run_changes_nothing(scenario: dict[str, Any]) -> None:
    dst = scenario["dst"]
    before = (records(dst), len(memory_events(dst)))

    result = asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=True))

    assert result.dry_run and result.deleted_again == 2
    assert (records(dst), len(memory_events(dst))) == before


def test_repair_refuses_the_live_database_and_bad_names(scenario: dict[str, Any]) -> None:
    src = scenario["src"]
    live_name = src.database_url.get_secret_value().rsplit("/", 1)[1]

    with pytest.raises(RepairRefused, match="live database"):
        asyncio.run(run(src, live_name, dry_run=True))
    with pytest.raises(RepairRefused, match="plain database name"):
        asyncio.run(run(src, "x; DROP DATABASE aether", dry_run=True))


def test_repair_refuses_an_unrelated_database(
    scenario: dict[str, Any], live_owner: Settings
) -> None:
    # The copy's events must be the live events up to its newest one.
    sql(scenario["src"], "INSERT INTO events (occurred_at, event_type, schema_version, actor)"
        " VALUES (now() - interval '1 year', 'request.received', 1, 'core')")  # fmt: skip
    dst = scenario["dst"]
    sql(dst, "INSERT INTO events (occurred_at, event_type, schema_version, actor)"
        " VALUES (now(), 'request.received', 1, 'core')")  # fmt: skip

    with pytest.raises(RepairRefused, match="not the live events"):
        asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=True))


def max_event_id(owner: Settings) -> int:
    return int(sql(owner, "SELECT coalesce(max(id), 0) FROM events")[0][0])


def test_migration_0006_downgrade_refuses_while_records_are_restored(
    scenario: dict[str, Any],
) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import Connection
    from sqlalchemy.exc import DBAPIError

    from tests.test_backup_live import REPO, owner_run

    dst = scenario["dst"]
    asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=False))
    config = Config(str(REPO / "alembic.ini"))

    def downgrade(sync: Connection) -> None:
        config.attributes["connection"] = sync
        command.downgrade(config, "0005")

    async def body(conn: Any) -> None:
        await conn.run_sync(downgrade)

    with pytest.raises(DBAPIError, match="restored memory records exist"):
        asyncio.run(owner_run(dst, body))
    assert sql(dst, "SELECT version_num FROM alembic_version")[0][0] == "0006"

    # Once reviewed, it goes down and up cleanly.
    sql(dst, "DELETE FROM memory_records WHERE restored")
    asyncio.run(owner_run(dst, body))
    assert sql(dst, "SELECT version_num FROM alembic_version")[0][0] == "0005"
    columns = sql(
        dst,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'memory_records' AND column_name = 'restored'",
    )
    assert columns == []


# --- review findings (security-reviewer, step 16) ---------------------------------


def _fingerprint_of(owner: Settings) -> tuple[Any, ...]:
    rows = sql(
        owner,
        "SELECT (SELECT count(*) FROM events),"
        " (SELECT md5(coalesce(string_agg(row_to_json(e)::text, ',' ORDER BY id), ''))"
        "  FROM events e),"
        " (SELECT md5(coalesce(string_agg(row_to_json(m)::text, ',' ORDER BY id), ''))"
        "  FROM memory_records m),"
        " (SELECT version_num FROM alembic_version)",
    )
    return tuple(rows[0])


def test_the_live_database_is_never_changed(scenario: dict[str, Any]) -> None:
    src = scenario["src"]
    before = _fingerprint_of(src)

    asyncio.run(run(src, scenario["dst_name"], dry_run=True))
    asyncio.run(run(src, scenario["dst_name"], dry_run=False))
    with pytest.raises(RepairRefused):
        asyncio.run(run(src, scenario["dst_name"], dry_run=False))

    assert _fingerprint_of(src) == before


def test_an_empty_database_is_refused_and_left_untouched(
    scenario: dict[str, Any], live_owner: Settings
) -> None:
    name = "aether_restore_empty_test"
    asyncio.run(owner_autocommit(live_owner, f"DROP DATABASE IF EXISTS {name}"))
    asyncio.run(owner_autocommit(live_owner, f"CREATE DATABASE {name}"))
    try:
        with pytest.raises(RepairRefused, match="not a restored AETHER database"):
            asyncio.run(run(scenario["src"], name, dry_run=False))
        tables = sql(
            with_database(live_owner, name),
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'",
        )
        assert tables[0][0] == 0  # nothing migrated into it
    finally:
        asyncio.run(owner_autocommit(live_owner, f"DROP DATABASE IF EXISTS {name}"))


def test_the_app_role_is_refused(scenario: dict[str, Any], live_app: Settings) -> None:
    # Only an app-role URL: migration_url() falls back to DATABASE_URL.
    app_on_src = with_database(live_app, scenario["src_name"]).model_copy(
        update={"database_migration_url": None}
    )
    with pytest.raises(RepairRefused, match="owner role"):
        asyncio.run(run(app_on_src, scenario["dst_name"], dry_run=True))


def test_an_older_copy_is_upgraded_only_after_the_checks(scenario: dict[str, Any]) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import Connection

    from tests.test_backup_live import REPO, owner_run

    dst = scenario["dst"]
    config = Config(str(REPO / "alembic.ini"))

    def downgrade(sync: Connection) -> None:
        config.attributes["connection"] = sync
        command.downgrade(config, "0005")

    async def body(conn: Any) -> None:
        await conn.run_sync(downgrade)

    asyncio.run(owner_run(dst, body))  # a backup taken before 0006

    with pytest.raises(RepairRefused, match="behind the current migration"):
        asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=True))
    assert sql(dst, "SELECT version_num FROM alembic_version")[0][0] == "0005"

    result = asyncio.run(run(scenario["src"], scenario["dst_name"], dry_run=False))

    assert sql(dst, "SELECT version_num FROM alembic_version")[0][0] == "0006"
    assert (result.deleted_again, result.set_pending) == (2, 3)


def test_the_cli_logs_counts_only(
    scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from core.log import configure_logging
    from memory.restore import main

    out = io.StringIO()
    real = configure_logging
    monkeypatch.setattr(
        "memory.restore.configure_logging", lambda s, stream=None: real(s, stream=out)
    )
    src = scenario["src"]

    code = main(["--into", scenario["dst_name"]], settings=src)

    log = out.getvalue()
    assert code == 0
    last = json.loads(log.strip().splitlines()[-1])
    assert last["message"] == "Memory repair completed"
    assert (last["deleted_again"], last["set_pending"]) == (2, 3)
    from core.db import migration_url

    _, secret = migration_url(src)
    assert secret is not None
    url = secret.get_secret_value()
    password = url.split("@", 1)[0].rsplit(":", 1)[1]
    for leaked in (CANARY, url, password):
        assert leaked not in log
