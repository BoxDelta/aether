"""The "restored" marker on memory records (ADR-024 §4, step 16 of #58; #67).

After a restore, the repair (``memory/restore.py``) sets records the owner had
approved or corrected after the backup back to ``pending``, so the owner re-checks
them. Those records are marked ``restored``: they are kept until the owner reviews
them, never expire, and don't count toward the 50-proposal limit.

What changes:

- ``memory_records.restored``: boolean, NOT NULL, default false. Existing rows get
  false (a constant default: a catalogue change, no table rewrite).
- CHECK ``memory_records_restored``: only a ``pending`` record can be restored.
  Approving one clears the marker (the store does that in the same UPDATE).

Unchanged: every other column, CHECK and index, and the app role's rights
(table-level grants cover the new column).

Downgrade: refuses, with an error and no change, while any record is restored
(dropping the column would silently make such a record expire and count again);
otherwise drops exactly the constraint and the column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "memory_records"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("restored", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "memory_records_restored", TABLE, "NOT restored OR status = 'pending'"
    )


def downgrade() -> None:
    found = f"SELECT 1 FROM {TABLE} WHERE restored"  # noqa: S608 - a constant
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS ({found}) THEN
                RAISE EXCEPTION 'downgrade refused: restored memory records exist; '
                    'review them (approve or reject) first';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint("memory_records_restored", TABLE, type_="check")
    op.drop_column(TABLE, "restored")
