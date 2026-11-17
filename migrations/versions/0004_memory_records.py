"""Memory records table (ADR-024 §1, step 3 of #58; #66).

One table, ``memory_records``: plain-text facts with a privacy level, a review
status and provenance. No store, API or events yet (ADR-024 §9, steps 4 to 6).

Constraints, all checked by the database:

- ``content``: 1 to 1,000 characters.
- ``key`` (optional): lowercase letters, digits, ``_`` and ``.``, 1 to 64
  characters; at most one *approved* record per key (a partial unique index).
- ``category``: one of ``CATEGORIES``.
- ``privacy``: NOT NULL and one of ``PRIVACY_LEVELS``, which is
  ``router.contract.NON_SECRET_LEVELS`` written out: ``secret`` is never stored.
  Migrations are frozen snapshots and don't import app code;
  ``tests/test_privacy_levels.py`` fails if the two drift.
- ``status``: ``pending`` or ``approved``. An approved record has ``approved_at``
  and a level the owner chose (``privacy_source = 'owner'``), so no record becomes
  approved with a level nobody chose (ADR-024 §1, §2); a pending one has no
  ``approved_at``.
- Provenance: ``source`` (owner, model, tool), ``source_provider`` and
  ``source_request_id`` (optional, same formats as in ``events``) and
  ``privacy_source`` (owner, suggested).
- ``created_at``, ``updated_at``, ``approved_at`` and the optional ``expires_at``.

Rights: the app role ``aether_app`` (0003) gets exactly SELECT, INSERT, UPDATE and
DELETE: Core will create, read, correct and hard-delete records (ADR-024 §4). No
TRUNCATE, TRIGGER or REFERENCES, and nothing for PUBLIC. The app role owns
nothing, so it can't alter or drop the table. Core's privilege check
(``core/db.py``) allows UPDATE and DELETE on this table only.

Creates exactly: table ``memory_records`` (+ its primary key, CHECK constraints
and the partial unique index ``memory_records_approved_key``). Downgrade drops
exactly that table; ``events`` is not touched.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aether_app"
TABLE = "memory_records"
CONTENT_MAX = 1000
CATEGORIES = ("profile", "preference", "project", "decision", "task", "reference")
PRIVACY_LEVELS = ("public", "personal", "sensitive")
STATUSES = ("pending", "approved")
SOURCES = ("owner", "model", "tool")
PRIVACY_SOURCES = ("owner", "suggested")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    now = sa.func.now()
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("key", sa.Text),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("privacy", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_provider", sa.Text),
        sa.Column("source_request_id", sa.Text),
        sa.Column("privacy_source", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            f"char_length(content) BETWEEN 1 AND {CONTENT_MAX}", name="memory_records_content"
        ),
        sa.CheckConstraint(r"key ~ '^[a-z0-9_.]{1,64}$'", name="memory_records_key"),
        sa.CheckConstraint(_in("category", CATEGORIES), name="memory_records_category"),
        sa.CheckConstraint(_in("privacy", PRIVACY_LEVELS), name="memory_records_privacy"),
        sa.CheckConstraint(_in("status", STATUSES), name="memory_records_status"),
        sa.CheckConstraint(_in("source", SOURCES), name="memory_records_source"),
        sa.CheckConstraint(
            r"source_provider ~ '^[a-z0-9_-]{1,32}$'", name="memory_records_source_provider"
        ),
        sa.CheckConstraint(
            r"source_request_id ~ '^[A-Za-z0-9._-]{1,128}$'",
            name="memory_records_source_request_id",
        ),
        sa.CheckConstraint(
            _in("privacy_source", PRIVACY_SOURCES), name="memory_records_privacy_source"
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND approved_at IS NOT NULL AND privacy_source = 'owner')"
            " OR (status = 'pending' AND approved_at IS NULL)",
            name="memory_records_approval",
        ),
    )
    op.create_index(
        "memory_records_approved_key",
        TABLE,
        ["key"],
        unique=True,
        postgresql_where=sa.text("status = 'approved' AND key IS NOT NULL"),
    )
    op.execute(f"REVOKE ALL ON {TABLE} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")


def downgrade() -> None:
    # Dropping the table drops its index, constraints and grants. Nothing else.
    op.drop_table(TABLE)
