# ADR-020: Postgres setup: Docker Compose, asyncpg, Alembic

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

ADR-011 chose one Postgres for memory, runtime tasks and events. ADR-017
named SQLAlchemy and Alembic. #20 needs the concrete setup: how the
database runs on the home PC, which driver Core uses, how the schema
changes, and how the password stays out of the repository, the logs and
any error text.

## Decision

-   **Runtime:** the official `postgres:18-alpine` image, pinned by digest,
    in `docker-compose.yml`. Database and user are both `aether`. The port
    is bound to `127.0.0.1:5432` only, so it isn't reachable from the LAN
    or over Tailscale. Data lives in the Docker named volume
    `aether_pgdata`. The container restarts with Docker
    (`unless-stopped`).
-   **Driver:** SQLAlchemy 2 async engine with `asyncpg`
    (`postgresql+asyncpg://`), matching FastAPI's async request path.
    Alternatives considered: psycopg 3 (async-capable, but asyncpg is the
    common SQLAlchemy async choice) and sync SQLAlchemy in a thread pool
    (simpler, but blocks workers).
-   **Migrations:** Alembic in `migrations/`, hand-written until models
    exist. The first revision (`0001`) is empty and only creates
    `alembic_version`.
-   **Secrets:** `POSTGRES_PASSWORD` (read by compose) and `DATABASE_URL`
    (read by Core and Alembic) live only in `.env`. `alembic.ini` has no
    URL. `DATABASE_URL` is a `SecretStr`. Its password is redacted by
    value, and any `scheme://user:password@host` URL is redacted by
    pattern. URL checks run in `core.db`, not as a pydantic validator,
    because pydantic keeps the raw input in `ValidationError.errors()`.
-   **Errors:** connection failures are classified (`auth_failed`,
    `unreachable`, `timeout`, `database_missing`, `misconfigured`) with
    messages built from safe parts only (user, host, port, database name).

## Consequences

-   One container and one volume to run and back up.
    `docker compose down -v` deletes all data.
-   The password is set when the volume is first created. Changing it in
    `.env` afterwards doesn't change the database password. Rotation needs
    `ALTER ROLE` (documented in the README).
-   Core connects as the database owner. A separate least-privilege
    application role is deferred until there are tables to protect.
-   Live database tests are opt-in locally (`AETHER_DB_TESTS=1`). Since
    #52, Sentinel runs them on every push and PR. It starts the
    `docker-compose.yml` service itself, so CI and the PC use one pinned
    digest (`tests/test_ci_postgres.py` fails on any other). The password
    is generated per run and masked, never stored in the repo or as a
    secret. Dependabot (`docker-compose` ecosystem) proposes digest and
    18.x updates; major versions are ignored.

## Addition (2026-11-16): least-privilege app role (#1, #12)

The deferred role above is now in place. The single-role text above is
kept as the original decision.

-   **Two roles.** The owner `aether` (created by the image, a
    superuser) runs migrations only, through `DATABASE_MIGRATION_URL`.
    Core connects as `aether_app` through `DATABASE_URL`. Alembic falls
    back to `DATABASE_URL` when `DATABASE_MIGRATION_URL` is unset, so a
    throwaway database with one URL (Sentinel) still migrates; on the PC
    that fallback would connect as the app role and fail safely.
-   **Created by a migration.** `0003` creates `aether_app` (LOGIN, no
    password, no special attributes) and grants exactly: CONNECT on the
    database, USAGE on the schema, SELECT and INSERT on `events`, SELECT
    on `alembic_version`. It revokes the default PUBLIC rights on the
    database, the schema and `events_append_only()`. Future tables
    (Memory, ADR-024) grant the app role what they need in their own
    migration. Default privileges only revoke PUBLIC EXECUTE on
    functions the owner creates later; nothing is granted by default, so
    nothing new reaches the app role by accident.
-   **Password.** Never in a migration. The owner sets it once with
    `uv run python -m core.db_admin set-app-password`, which takes it
    from `DATABASE_URL` and sends only a SCRAM-SHA-256 verifier (as
    `psql`'s `\password` does), with `log_statement` off for that
    statement. It only changes `aether_app` (or a member of it) without
    special attributes, never another role named by mistake. Alternatives considered: `\password`
    by hand (interactive, easy to mistype) and `ALTER ROLE ... PASSWORD
    '<plain>'` (the plain password could reach the server log).
-   **Guard.** Every new connection of Core's engine runs one query and
    refuses a role with special attributes, the owner of the database or
    of any non-system table or function, a role that can create in the
    database or any schema, a member of `pg_write_all_data` or the
    server-files/program roles, or one with UPDATE, DELETE, TRUNCATE or
    TRIGGER on any table. Core then refuses to start and `/ready` reports
    `misconfigured`. Detecting by privilege, not by name, also catches
    a renamed or wrongly granted role.
-   **Minimum password length** of 8 characters for both URLs, the same
    threshold as redaction by value.
-   **Existing installs** move with `docs/runbooks/db-app-role.md`: no
    table or volume is dropped or rewritten.

Consequences: one more secret in `.env`, and one more setup step. Both
passwords still sit in the same `.env`, so this protects the events
against Core's own bugs and a leaked app-role password, not against
someone who can read `.env` or the disk (#57). Live tests create and
drop a temporary member role of `aether_app` for each test.

## Addition (2026-11-16): memory_records grants and the guard (#66)

ADR-024 §1 adds `memory_records`, the first table Core must change and
hard-delete (ADR-024 §4). The role and guard above are kept, with one
narrow exception.

-   **Grants.** Migration `0004` gives `aether_app` exactly SELECT,
    INSERT, UPDATE and DELETE on `memory_records`. No TRUNCATE, TRIGGER
    or REFERENCES, nothing for PUBLIC, and the owner still owns the
    table.
-   **Guard.** The privilege check still refuses TRUNCATE and TRIGGER on
    every table, and owning or creating anything anywhere. It now refuses
    UPDATE (on the table or on any single column) and DELETE on every
    table **except** `public.memory_records`. So `events`
    and `alembic_version` stay insert/read-only for Core, and a role with
    UPDATE or DELETE on them is still refused (tested live, as is the
    real app role with the `0004` grants being accepted). The check for
    column-level UPDATE is new; before, only table-level UPDATE was seen.
-   **Why by name and schema.** The exception is one fixed table,
    `memory_records` in the `public` schema (where the migrations create
    it; `/ready` already assumes `public`), not a pattern or a list read
    from the database. The app role can't create tables, so it can't
    make another one. A same-named table in another schema (which only
    the owner could create) is not exempt, and neither is
    `memory_records_x` (both tested live). The memory store (step 4)
    names the table with its schema. Alternatives considered: a second
    check only for `events` and `alembic_version` (would allow UPDATE on
    any future table by default), and per-table row-level security
    (more machinery than one table needs).

## Addition (2026-11-17): data on the encrypted volume (#57, ADR-026)

The runtime above (the named volume `aether_pgdata`) is kept as the
default, and as the original decision. ADR-026 adds an opt-in override,
`compose.vault.yaml`. The owner turns it on with
`COMPOSE_FILE=docker-compose.yml:compose.vault.yaml` in `.env` after
running `docs/runbooks/encrypted-volume.md`. With it:

-   **Data.** The data lives on a LUKS2 container file, bind-mounted from
    `~/.local/share/aether/vault/data`, with `create_host_path: false`. A
    locked volume means Docker refuses to start the container; nothing is
    created on the bare disk.
-   **Marker.** The entrypoint refuses to start Postgres unless the
    volume's marker `/var/lib/postgresql/.aether-vault` is present, so a
    fresh database is never initialised outside the volume.
-   **Restart policy.** `restart: "no"`: Postgres is started by
    `aether-vault open` after the unlock, not by Docker at boot. This
    replaces `unless-stopped` for this setup.
-   **Logs.** Postgres runs with `log_error_verbosity=terse` and
    `log_min_error_statement=panic`, so errors in Docker's log (on the
    unencrypted disk) don't quote rows, keys or statements.
-   **Unchanged.** The image, user, database, port binding and healthcheck.
    Sentinel keeps the default.

A later small PR makes the bind mount the default and gives Sentinel its
own named-volume override (ADR-026 §9).

## Revisit when

Backups are designed (Phase 2), pgvector is needed (ADR-011), a
least-privilege role is added, or AETHER moves off the home PC.
