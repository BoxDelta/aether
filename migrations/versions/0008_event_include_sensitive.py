"""Events record the per-request approval for Sensitive memory (#144, ADR-024 §3).

``include_sensitive`` is the owner's per-request approval for Sensitive memory
(docs/07 §6). Until now no event recorded it, so a request that approved Sensitive
memory but got none left no trace (docs/05 §9).

What changes:

- ``events.include_sensitive``: boolean, nullable, no default (a catalogue change,
  no table rewrite). Set on the request-level events (``request.received``,
  ``model.route.selected``, ``request.failed``), like ``allow_cloud``.
- CHECK ``events_include_sensitive_shape``: never set on a memory event (those carry
  only their own typed columns, ADR-024 §5).

Unchanged: every other column, CHECK, index and trigger, and the app role's rights
(table-level grants cover the new column).

Downgrade: refuses, with an error and no change, while any event records
``include_sensitive`` (events is append-only audit data); otherwise drops exactly
the constraint and the column.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "events"
COLUMN = "include_sensitive"
CHECK = "events_include_sensitive_shape"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.Boolean))
    op.create_check_constraint(
        CHECK, TABLE, f"{COLUMN} IS NULL OR NOT starts_with(event_type, 'memory.')"
    )


def downgrade() -> None:
    # events is append-only audit data: never drop recorded approvals silently.
    found = f"SELECT 1 FROM {TABLE} WHERE {COLUMN} IS NOT NULL"  # noqa: S608 - constants
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS ({found}) THEN
                RAISE EXCEPTION 'downgrade refused: events record include_sensitive; '
                    'dropping the column would delete audit data';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
