"""Memory events on the events table (ADR-024 §5, step 5 of #58; #72).

Metadata only, like the rest of ``events``: typed columns, each with a CHECK, no
JSON field and no column that memory content, a key, corrected text, a search
query or a rejection reason could go into.

What changes:

- ``event_type`` also allows the eight ``MEMORY_EVENT_TYPES``.
- ``actor`` also allows ``memory`` (Core's memory code) and ``restore`` (the restore
  runbook, ADR-024 §4). A ``memory.*`` event has actor ``memory`` or ``restore``,
  and every other event keeps ``core`` or ``router``.
- New nullable columns (no default, so existing rows are not rewritten):

  - every record event (``RECORD_EVENT_TYPES``): ``memory_record_id``,
    ``memory_category``, ``memory_source`` and ``memory_level``, all four required;
    ``memory_old_level`` only on ``memory.updated``, and only when the level changed;
  - ``memory.context.retrieved``: ``request_id`` and ``provider`` (existing
    columns), a count per level (``memory_count_<level>``), ``memory_chars``,
    ``memory_effective_level``, ``memory_record_ids`` (at most 20 UUIDs, one per
    record sent, so the count matches the per-level counts) and
    ``memory_left_out``, all required;
  - ``memory.exported``: ``memory_export_count``, required.

  Every new column is NULL on every event type it doesn't belong to, so the rows
  written before this migration stay valid.
- Level columns take ``LEVELS``, which is ``router.contract.NON_SECRET_LEVELS``
  written out: ``secret`` is never stored in memory, and a call that carries memory
  never has an effective level of ``secret`` (a Secret request is never sent to any
  model). ``tests/test_privacy_levels.py`` fails if the copy drifts. The effective
  level is at least the highest level with a non-zero count.

Unchanged: the append-only triggers, every index, and the app role's rights
(INSERT and SELECT on ``events``; table-level grants cover the new columns).

Downgrade: events are permanent (append-only), so it refuses, with an error and no
change, while any ``memory.*`` event or any ``memory``/``restore`` actor exists. It
never deletes an audit row. On a table without them it drops exactly the new
constraints and columns and restores the 0002 ``event_type`` and ``actor`` CHECKs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "events"
# Frozen copies (migrations don't import app code); tests/test_memory_events.py and
# tests/test_privacy_levels.py fail if they drift from core/, memory/ and router/.
BASE_EVENT_TYPES = (
    "request.received",
    "model.route.selected",
    "model.call.completed",
    "provider.unavailable",
    "request.failed",
)
RECORD_EVENT_TYPES = (
    "memory.proposed",
    "memory.approved",
    "memory.rejected",
    "memory.updated",
    "memory.deleted",
    "memory.expired",
)
EXPORTED = "memory.exported"
RETRIEVED = "memory.context.retrieved"
MEMORY_EVENT_TYPES = (*RECORD_EVENT_TYPES, EXPORTED, RETRIEVED)
UPDATED = "memory.updated"
BASE_ACTORS = ("core", "router")
MEMORY_ACTORS = ("memory", "restore")
CATEGORIES = ("profile", "preference", "project", "decision", "task", "reference")
SOURCES = ("owner", "model", "tool")
LEVELS = ("public", "personal", "sensitive")
MAX_RECORD_IDS = 20

COUNT_COLUMNS = tuple(f"memory_count_{level}" for level in LEVELS)
RECORD_COLUMNS = ("memory_record_id", "memory_category", "memory_source", "memory_level")
RETRIEVED_COLUMNS = (
    *COUNT_COLUMNS,
    "memory_chars",
    "memory_effective_level",
    "memory_record_ids",
    "memory_left_out",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _nonnulls(columns: Sequence[str]) -> str:
    return f"num_nonnulls({', '.join(columns)})"


def _effective_covers_counts() -> str:
    # For each level: no records at that level, or an effective level at least as high.
    return " AND ".join(
        f"({count} = 0 OR {_in('memory_effective_level', LEVELS[i:])})"
        for i, count in enumerate(COUNT_COLUMNS)
    )


CHECKS: dict[str, str] = {
    "events_memory_actor": (
        f"({_in('event_type', MEMORY_EVENT_TYPES)}) = ({_in('actor', MEMORY_ACTORS)})"
    ),
    "events_memory_category": _in("memory_category", CATEGORIES),
    "events_memory_source": _in("memory_source", SOURCES),
    "events_memory_level": _in("memory_level", LEVELS),
    "events_memory_old_level": _in("memory_old_level", LEVELS),
    "events_memory_effective_level": _in("memory_effective_level", LEVELS),
    "events_memory_counts": " AND ".join(f"{c} >= 0" for c in COUNT_COLUMNS),
    "events_memory_chars": "memory_chars >= 0",
    "events_memory_left_out": "memory_left_out >= 0",
    "events_memory_export_count": "memory_export_count >= 0",
    "events_memory_record_ids": (
        f"cardinality(memory_record_ids) <= {MAX_RECORD_IDS}"
        " AND array_position(memory_record_ids, NULL) IS NULL"
        " AND coalesce(array_ndims(memory_record_ids), 1) = 1"
    ),
    # Which columns each event type carries: all of them, or none.
    "events_memory_record_shape": (
        f"{_nonnulls(RECORD_COLUMNS)} = CASE WHEN {_in('event_type', RECORD_EVENT_TYPES)}"
        f" THEN {len(RECORD_COLUMNS)} ELSE 0 END"
    ),
    "events_memory_old_level_shape": (
        f"memory_old_level IS NULL OR (event_type = '{UPDATED}'"
        " AND memory_old_level <> memory_level)"
    ),
    "events_memory_retrieved_shape": (
        f"CASE WHEN event_type = '{RETRIEVED}'"
        f" THEN {_nonnulls(RETRIEVED_COLUMNS)} = {len(RETRIEVED_COLUMNS)}"
        " AND request_id IS NOT NULL AND provider IS NOT NULL"
        f" AND cardinality(memory_record_ids) = {' + '.join(COUNT_COLUMNS)}"
        f" AND {_effective_covers_counts()}"
        f" ELSE {_nonnulls(RETRIEVED_COLUMNS)} = 0 END"
    ),
    "events_memory_exported_shape": (
        f"(memory_export_count IS NOT NULL) = (event_type = '{EXPORTED}')"
    ),
}


def _columns() -> list[sa.Column[Any]]:
    text_columns = (
        "memory_category",
        "memory_source",
        "memory_level",
        "memory_old_level",
        "memory_effective_level",
    )
    integer_columns = (*COUNT_COLUMNS, "memory_chars", "memory_left_out", "memory_export_count")
    return [
        sa.Column("memory_record_id", postgresql.UUID(as_uuid=True)),
        *(sa.Column(name, sa.Text) for name in text_columns),
        *(sa.Column(name, sa.Integer) for name in integer_columns),
        sa.Column("memory_record_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True))),
    ]


COLUMNS = tuple(c.name for c in _columns())


def upgrade() -> None:
    # Nullable columns without a default: a catalogue change, no table rewrite.
    for column in _columns():
        op.add_column(TABLE, column)
    op.drop_constraint("events_event_type", TABLE, type_="check")
    op.create_check_constraint(
        "events_event_type", TABLE, _in("event_type", (*BASE_EVENT_TYPES, *MEMORY_EVENT_TYPES))
    )
    op.drop_constraint("events_actor", TABLE, type_="check")
    op.create_check_constraint("events_actor", TABLE, _in("actor", (*BASE_ACTORS, *MEMORY_ACTORS)))
    for name, condition in CHECKS.items():
        op.create_check_constraint(name, TABLE, condition)


def downgrade() -> None:
    # Events are append-only: never delete audit rows to make a downgrade fit.
    memory_rows = f"{_in('event_type', MEMORY_EVENT_TYPES)} OR {_in('actor', MEMORY_ACTORS)}"
    found = f"SELECT 1 FROM {TABLE} WHERE {memory_rows}"  # noqa: S608 - constants only
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS ({found}) THEN
                RAISE EXCEPTION 'downgrade refused: events holds memory events, which are '
                    'permanent; restore a backup from before 0005 instead';
            END IF;
        END
        $$
        """
    )
    for name in reversed(CHECKS):
        op.drop_constraint(name, TABLE, type_="check")
    op.drop_constraint("events_actor", TABLE, type_="check")
    op.create_check_constraint("events_actor", TABLE, _in("actor", BASE_ACTORS))
    op.drop_constraint("events_event_type", TABLE, type_="check")
    op.create_check_constraint("events_event_type", TABLE, _in("event_type", BASE_EVENT_TYPES))
    for name in reversed(COLUMNS):
        op.drop_column(TABLE, name)
