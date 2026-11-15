"""Append-only events table (#21, ADR-011).

Metadata only: typed columns, each constrained, and no free-form JSON blob, so
there is no column a prompt, answer, memory record or traceback fits into.
UPDATE, DELETE and TRUNCATE are refused by a trigger.

Creates exactly: table ``events`` (+ its identity sequence, primary key, CHECK
constraints and two indexes), function ``events_append_only()`` and triggers
``events_no_update_delete`` / ``events_no_truncate``. Downgrade drops exactly
those. Names are unqualified, so they land in the current schema.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = (
    "request.received",
    "model.route.selected",
    "model.call.completed",
    "provider.unavailable",
    "request.failed",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("schema_version", sa.SmallInteger, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text),
        sa.Column("request_id", sa.Text),
        sa.Column("session_id", sa.Text),
        sa.Column("privacy", sa.Text),
        sa.Column("allow_cloud", sa.Boolean),
        sa.Column("override", sa.Text),
        sa.Column("provider", sa.Text),
        sa.Column("provider_kind", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("route_reason", sa.Text),
        sa.Column("finish_reason", sa.Text),
        sa.Column("latency_ms", sa.Double),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("error_type", sa.Text),
        sa.Column("error_message", sa.Text),
        sa.Column("attempts", postgresql.JSONB),
        sa.CheckConstraint(_in("event_type", EVENT_TYPES), name="events_event_type"),
        sa.CheckConstraint("schema_version >= 1", name="events_schema_version"),
        sa.CheckConstraint(_in("actor", ("core", "router")), name="events_actor"),
        sa.CheckConstraint(_in("outcome", ("ok", "refusal", "error")), name="events_outcome"),
        sa.CheckConstraint(r"request_id ~ '^[A-Za-z0-9._-]{1,128}$'", name="events_request_id"),
        sa.CheckConstraint(r"session_id ~ '^[A-Za-z0-9._-]{1,128}$'", name="events_session_id"),
        sa.CheckConstraint(
            _in("privacy", ("public", "personal", "sensitive", "secret")), name="events_privacy"
        ),
        sa.CheckConstraint(r"override ~ '^[a-z0-9_-]{1,32}$'", name="events_override"),
        sa.CheckConstraint(r"provider ~ '^[a-z0-9_-]{1,32}$'", name="events_provider"),
        sa.CheckConstraint(_in("provider_kind", ("local", "cloud")), name="events_provider_kind"),
        sa.CheckConstraint(r"model ~ '^[A-Za-z0-9._:/-]{1,200}$'", name="events_model"),
        sa.CheckConstraint("char_length(route_reason) <= 500", name="events_route_reason"),
        sa.CheckConstraint(
            _in("finish_reason", ("stop", "length", "refusal", "other")),
            name="events_finish_reason",
        ),
        sa.CheckConstraint("latency_ms >= 0", name="events_latency_ms"),
        sa.CheckConstraint("input_tokens >= 0", name="events_input_tokens"),
        sa.CheckConstraint("output_tokens >= 0", name="events_output_tokens"),
        sa.CheckConstraint(
            r"error_type ~ '^[A-Za-z_][A-Za-z0-9_]{0,63}$'", name="events_error_type"
        ),
        sa.CheckConstraint("char_length(error_message) <= 500", name="events_error_message"),
        sa.CheckConstraint(
            "jsonb_typeof(attempts) = 'array' AND jsonb_array_length(attempts) <= 2",
            name="events_attempts",
        ),
    )
    op.create_index("events_occurred_at_idx", "events", ["occurred_at"])
    op.create_index("events_request_id_idx", "events", ["request_id"])
    op.execute(
        """
        CREATE FUNCTION events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'events is append-only (% refused)', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER events_no_update_delete BEFORE UPDATE OR DELETE ON events "
        "FOR EACH ROW EXECUTE FUNCTION events_append_only()"
    )
    op.execute(
        "CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events "
        "FOR EACH STATEMENT EXECUTE FUNCTION events_append_only()"
    )


def downgrade() -> None:
    # Dropping the table drops its triggers, indexes, constraints and sequence.
    op.drop_table("events")
    op.execute("DROP FUNCTION events_append_only()")
