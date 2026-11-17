"""Least-privilege app role for Core (#1, #12, ADR-020 addition 2026-11-16).

Creates role ``aether_app`` if it doesn't exist, with LOGIN but no password: it
can't log in until the owner sets one (``uv run python -m core.db_admin
set-app-password``, see docs/runbooks/db-app-role.md). Its rights, all explicit:

- CONNECT on the current database and USAGE on the current schema;
- SELECT and INSERT on ``events`` (INSERT ... RETURNING id needs SELECT);
- SELECT on ``alembic_version`` (``/ready`` compares it with the newest revision).

Nothing else: no UPDATE, DELETE or TRUNCATE on ``events``, and it owns nothing,
so it can't drop or disable the append-only triggers or alter or drop tables. It
can't create objects either: the default PUBLIC rights on the database (CONNECT,
TEMPORARY), on the schema (USAGE) and on ``events_append_only()`` (EXECUTE) are
revoked, so every right is granted to a named role. Triggers fire without
EXECUTE on their function. The owner (a superuser in the Docker image) keeps
running migrations.

Existing rows are never touched: no table is dropped, recreated or rewritten.

Downgrade revokes the app role's rights and restores the PUBLIC defaults. It
leaves the role itself, which is cluster-wide and may hold rights elsewhere
(another schema or database); drop it by hand (runbook, "Going back").

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aether_app"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN;
            END IF;
        END
        $$
        """
    )
    # Also corrects a role that existed before with more attributes. The password,
    # if one is set, stays as it is.
    op.execute(
        f"ALTER ROLE {APP_ROLE} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {APP_ROLE}', current_database());
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', current_schema());
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO {APP_ROLE}', current_schema());
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON events FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT ON events TO {APP_ROLE}")
    op.execute("REVOKE ALL ON alembic_version FROM PUBLIC")
    op.execute(f"GRANT SELECT ON alembic_version TO {APP_ROLE}")
    op.execute("REVOKE ALL ON FUNCTION events_append_only() FROM PUBLIC")


def downgrade() -> None:
    op.execute("GRANT EXECUTE ON FUNCTION events_append_only() TO PUBLIC")
    op.execute(f"REVOKE ALL ON alembic_version FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON events FROM {APP_ROLE}")
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM {APP_ROLE}', current_schema());
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM {APP_ROLE}', current_database());
            -- Postgres defaults: CONNECT and TEMPORARY on a database for PUBLIC, and
            -- USAGE on the "public" schema (not on any other schema).
            EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO PUBLIC',
                           current_database());
            IF current_schema() = 'public' THEN
                GRANT USAGE ON SCHEMA public TO PUBLIC;
            END IF;
        END
        $$
        """
    )
