"""Repair memory after a restore (ADR-024 §4, step 16 of #58; #3, #67).

Restoring a backup brings back memory as it was when the backup was taken:
records deleted later come back, a level raised later goes back down, corrected
text comes back. The events table is restored too, so the ``memory.*`` events
written after the backup are missing. This module repairs the restored copy from
the live database, **before** the swap (docs/runbooks/db-backup-restore.md §4):

1.  It reads every ``memory.*`` event the live database has after the restored
    copy's newest event (metadata only, no content).
2.  It deletes again, in the copy, every record with a saved ``memory.deleted``,
    ``memory.rejected`` or ``memory.expired`` event.
3.  It sets every other record with a saved ``memory.approved`` or
    ``memory.updated`` event back to ``pending``, at the higher of its restored
    level and the newest saved level, and marks it ``restored`` (migration 0006):
    the owner re-checks its text, since corrected text can't be recovered from
    events. A restored record never expires and doesn't count toward the
    50-proposal limit.
4.  It inserts the saved events again (new ``id`` and ``recorded_at``;
    ``occurred_at`` kept), then its own ``memory.deleted`` / ``memory.updated``
    events with ``actor="restore"``.

All in one transaction on the copy; the live database is only read. Before
anything changes, read-only checks: both connections are on the databases they
should be (``current_database()``), as the role that owns the tables (the owner,
``DATABASE_MIGRATION_URL``; never the app role), the copy is a restored AETHER
database with events, never ``postgres``/``template0``/``template1``, and its
events are exactly the live events up to its newest one (the runbook R4 check, by
fingerprint), so it can't run against the wrong database or twice. Only then is
the copy upgraded to the current migration head (a backup can be older). A dry
run changes nothing, so it refuses a copy that is behind head.

Records created after the backup are not in the copy and are lost; so are the
non-memory events after it (requests, routing). The result says how many.

Run (fish), with Core stopped:
    uv run python -m memory.restore --into aether_restore_check --dry-run
    uv run python -m memory.restore --into aether_restore_check
"""

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import Connection, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.config import Settings, get_settings
from core.db import create_migration_engine, migration_url
from core.events import (
    MEMORY_EVENT_TYPES,
    MemoryRecordMeta,
    events_table,
    memory_record_event,
    write_memory_event,
)
from core.log import configure_logging
from router.contract import PrivacyLevel, privacy_rank

logger = logging.getLogger("aether.memory.restore")

REPO = Path(__file__).resolve().parent.parent
DATABASE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
# Never a restored copy: the cluster's own databases.
RESERVED = frozenset({"postgres", "template0", "template1"})
GONE = ("memory.deleted", "memory.rejected", "memory.expired")
CHANGED = ("memory.approved", "memory.updated")
_FINGERPRINT = text(
    "SELECT count(*), md5(coalesce(string_agg("
    "id::text || ':' || event_type || ':' || extract(epoch FROM occurred_at)::text,"
    " ',' ORDER BY id), '')) FROM events WHERE id <= :upto"
)


class RepairRefused(Exception):
    """The repair can't run safely. The message is fixed text, safe to log."""


@dataclass(frozen=True, slots=True)
class RepairResult:
    events_copied: int  # memory.* events from after the backup, inserted again
    deleted_again: int  # records with a saved deleted/rejected/expired event
    set_pending: int  # records set back to pending and marked restored
    restored_total: int  # restored records in the copy now (owner to review)
    other_events_lost: int  # non-memory events after the backup (not copied)
    dry_run: bool


def with_database(url: str, database: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{database}"))


def owner_settings(settings: Settings, database: str | None = None) -> Settings:
    """Settings whose only URL is the owner URL, on ``database`` if given. Both URL
    fields are set explicitly, so an exported ``DATABASE_MIGRATION_URL`` in the
    environment can't override the target (it would point at the live database)."""
    _, url = migration_url(settings)
    if url is None:
        raise RepairRefused("DATABASE_MIGRATION_URL (or DATABASE_URL) is not set")
    value = url.get_secret_value()
    if database is not None:
        value = with_database(value, database)
    return Settings(_env_file=None, database_url=None, database_migration_url=SecretStr(value))


def database_of(settings: Settings) -> str:
    _, url = migration_url(settings)
    if url is None:
        raise RepairRefused("DATABASE_MIGRATION_URL (or DATABASE_URL) is not set")
    return urlsplit(url.get_secret_value()).path.lstrip("/")


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """Run the migrations on ``engine``'s database (the copy, never the live one)."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO / "alembic.ini"))

    def upgrade(sync: Connection) -> None:
        config.attributes["connection"] = sync
        command.upgrade(config, "head")

    async with engine.begin() as conn:
        await conn.run_sync(upgrade)


async def _fingerprint(conn: AsyncConnection, upto: int) -> tuple[int, str]:
    row = (await conn.execute(_FINGERPRINT, {"upto": upto})).one()
    return int(row[0]), str(row[1])


async def _read_saved(
    source: AsyncEngine, target_max: int, target_print: tuple[int, str]
) -> tuple[list[dict[str, Any]], int]:
    """The live memory.* events after ``target_max``, and how many other events."""
    async with source.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        if await _fingerprint(conn, target_max) != target_print:
            raise RepairRefused(
                "the copy's events are not the live events up to its newest one: "
                "wrong database, wrong backup, or already repaired"
            )
        after = events_table.c.id > target_max
        rows = await conn.execute(
            select(events_table)
            .where(after, events_table.c.event_type.in_(MEMORY_EVENT_TYPES))
            .order_by(events_table.c.id)
        )
        saved = [dict(r._mapping) for r in rows]
        total = await conn.scalar(
            text("SELECT count(*) FROM events WHERE id > :m"), {"m": target_max}
        )
        await conn.rollback()
    return saved, int(total or 0) - len(saved)


def _plan(saved: Sequence[dict[str, Any]]) -> tuple[list[UUID], dict[UUID, PrivacyLevel]]:
    """Records to delete again, and records to set back with their newest saved level."""
    gone: list[UUID] = []
    newest: dict[UUID, PrivacyLevel] = {}
    for event in saved:
        record_id = event["memory_record_id"]
        if record_id is None:
            continue
        if event["event_type"] in GONE and record_id not in gone:
            gone.append(record_id)
        elif event["event_type"] in CHANGED:
            newest[record_id] = cast(PrivacyLevel, event["memory_level"])
    for record_id in gone:
        newest.pop(record_id, None)
    return gone, newest


def _meta(row: Any) -> MemoryRecordMeta:
    return MemoryRecordMeta(row.id, row.category, row.source, row.privacy)


async def _apply(
    conn: AsyncConnection,
    saved: Sequence[dict[str, Any]],
    gone: Sequence[UUID],
    newest: dict[UUID, PrivacyLevel],
) -> tuple[int, int]:
    for saved_event in saved:
        values = {k: v for k, v in saved_event.items() if k not in ("id", "recorded_at")}
        await conn.execute(insert(events_table).values(**values))

    deleted = 0
    for record_id in gone:
        row = (
            await conn.execute(
                text(
                    "DELETE FROM memory_records WHERE id = :id"
                    " RETURNING id, category, source, privacy"
                ),
                {"id": record_id},
            )
        ).one_or_none()
        if row is None:  # created after the backup: not in the copy
            continue
        deleted += 1
        event = memory_record_event("memory.deleted", _meta(row), actor="restore")
        await write_memory_event(conn, event)

    pending = 0
    for record_id, saved_level in newest.items():
        current = (
            await conn.execute(
                text("SELECT privacy FROM memory_records WHERE id = :id FOR UPDATE"),
                {"id": record_id},
            )
        ).one_or_none()
        if current is None:
            continue
        old: PrivacyLevel = current.privacy
        level = saved_level if privacy_rank(saved_level) > privacy_rank(old) else old
        row = (
            await conn.execute(
                text(
                    "UPDATE memory_records SET status = 'pending', approved_at = NULL,"
                    " restored = true, privacy = :level, updated_at = now() WHERE id = :id"
                    " RETURNING id, category, source, privacy"
                ),
                {"id": record_id, "level": level},
            )
        ).one()
        pending += 1
        event = memory_record_event(
            "memory.updated",
            _meta(row),
            old_level=old if old != level else None,
            actor="restore",
        )
        await write_memory_event(conn, event)
    return deleted, pending


_PREFLIGHT = text(
    "SELECT current_database(),"
    " to_regclass('public.events') IS NOT NULL,"
    " to_regclass('public.alembic_version') IS NOT NULL,"
    " (SELECT tableowner = current_user FROM pg_tables"
    "  WHERE schemaname = 'public' AND tablename = 'events')"
)


async def preflight(source: AsyncEngine, target: AsyncEngine, *, live: str, into: str) -> None:
    """Read-only checks before anything changes (the upgrade included): the right
    databases, the owner role, a restored AETHER copy with events, and the copy's
    events are the live ones up to its newest. Raises ``RepairRefused``."""
    async with source.connect() as conn:
        name, has_events, _, owner = (await conn.execute(_PREFLIGHT)).one()
        await conn.rollback()
    if name != live or not has_events:
        raise RepairRefused("the live database is not the configured AETHER database")
    if not owner:
        raise RepairRefused("run as the owner role (DATABASE_MIGRATION_URL), not the app role")
    async with target.connect() as conn:
        name, has_events, has_version, owner = (await conn.execute(_PREFLIGHT)).one()
        if name != into or name == live:
            raise RepairRefused("the copy is not the database given with --into")
        if not (has_events and has_version):
            raise RepairRefused("the copy is not a restored AETHER database")
        if not owner:
            raise RepairRefused("run as the owner role (DATABASE_MIGRATION_URL), not the app role")
        target_max = int(await conn.scalar(text("SELECT coalesce(max(id), 0) FROM events")) or 0)
        if target_max == 0:
            raise RepairRefused("the copy has no events: not a restored backup")
        target_print = await _fingerprint(conn, target_max)
        await conn.rollback()
    await _read_saved(source, target_max, target_print)  # raises on a mismatch


async def at_head(engine: AsyncEngine) -> bool:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini"))).get_current_head()
    async with engine.connect() as conn:
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        await conn.rollback()
    return bool(version == head)


async def repair(
    source: AsyncEngine, target: AsyncEngine, *, dry_run: bool = False
) -> RepairResult:
    """Repair ``target`` (the restored copy) from ``source`` (the live database).
    ``dry_run``: do everything, report, and roll back."""
    async with target.connect() as conn:
        target_max = int(await conn.scalar(text("SELECT coalesce(max(id), 0) FROM events")) or 0)
        target_print = await _fingerprint(conn, target_max)
    saved, other = await _read_saved(source, target_max, target_print)
    gone, newest = _plan(saved)

    async with target.connect() as conn:
        tx = await conn.begin()
        try:
            deleted, pending = await _apply(conn, saved, gone, newest)
            restored = int(
                await conn.scalar(text("SELECT count(*) FROM memory_records WHERE restored")) or 0
            )
        except BaseException:
            await tx.rollback()
            raise
        if dry_run:
            await tx.rollback()
        else:
            await tx.commit()
    return RepairResult(
        events_copied=len(saved),
        deleted_again=deleted,
        set_pending=pending,
        restored_total=restored,
        other_events_lost=other,
        dry_run=dry_run,
    )


async def run(settings: Settings, into: str, *, dry_run: bool) -> RepairResult:
    if not DATABASE_NAME.fullmatch(into):
        raise RepairRefused("--into must be a plain database name")
    if into in RESERVED:
        raise RepairRefused("--into is one of the cluster's own databases")
    live = database_of(settings)
    if into == live:
        raise RepairRefused("--into is the live database; give the restored copy")
    source = create_migration_engine(owner_settings(settings))
    target = create_migration_engine(owner_settings(settings, into))
    try:
        await preflight(source, target, live=live, into=into)
        if not await at_head(target):
            if dry_run:
                raise RepairRefused(
                    "the copy is behind the current migration; a dry run changes nothing, "
                    "so run without --dry-run (it upgrades the copy first)"
                )
            await upgrade_to_head(target)  # the copy only, after every check passed
        return await repair(source, target, dry_run=dry_run)
    finally:
        await source.dispose()
        await target.dispose()


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m memory.restore", description=__doc__)
    parser.add_argument("--into", required=True, help="the restored copy (not the live database)")
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; change nothing (copy must be at head)"
    )
    args = parser.parse_args(argv)
    try:
        settings = settings if settings is not None else get_settings()
    except Exception as exc:
        configure_logging(Settings(_env_file=None))
        logger.error(
            "Memory repair failed", extra={"stage": "config", "error_type": type(exc).__name__}
        )
        return 1
    configure_logging(settings)
    try:
        result = asyncio.run(run(settings, args.into, dry_run=args.dry_run))
    except RepairRefused as exc:
        logger.error("Memory repair refused: %s", exc)
        return 1
    except Exception as exc:  # one safe line: no traceback, no URL
        logger.error("Memory repair failed", extra={"error_type": type(exc).__name__})
        return 1
    logger.info("Memory repair completed", extra=asdict(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
