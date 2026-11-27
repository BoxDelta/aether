"""Only the owner role may write ``actor = 'restore'`` events (#150, ADR-024 §4).

``actor = 'restore'`` marks the events the restore repair (``memory/restore.py``,
step 16) writes into a restored copy. Until now it was only a label: Core's app
role (``aether_app``) could write it too. The repair runs as the owner role (the
role that owns ``events``); Core never writes this actor.

What changes:

- Function ``events_restore_actor_owner_only()`` and the trigger
  ``events_restore_actor_owner_only`` (BEFORE INSERT, each row): an insert with
  ``actor = 'restore'`` is refused unless the connecting role can act as the owner
  of ``events`` (``pg_has_role(current_user, <owner>, 'USAGE')``: the owner itself,
  or a superuser). The error is a ``check_violation`` (SQLSTATE 23514), like the
  CHECKs that refuse other bad rows, and names no value. The catalog names are
  schema-qualified and the function runs with ``search_path = pg_catalog,
  pg_temp`` (``pg_temp`` last, so a temporary table can't shadow ``pg_class``;
  roles have no TEMPORARY right anyway, migration 0003).

Unchanged: the table, its columns, CHECKs, indexes and the append-only triggers,
and every grant. The app role still can't drop or disable triggers (it owns
nothing).

Downgrade: drops exactly the trigger and the function. Rows are untouched.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "events_restore_actor_owner_only"
TRIGGER = "events_restore_actor_owner_only"


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION events_restore_actor_owner_only() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF NEW.actor = 'restore' AND NOT pg_catalog.pg_has_role(
                current_user,
                (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID),
                'USAGE'
            ) THEN
                RAISE EXCEPTION 'actor restore is for the owner role only (the restore repair)'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {TRIGGER} BEFORE INSERT ON events "
        f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {TRIGGER} ON events")
    op.execute(f"DROP FUNCTION {FUNCTION}()")
