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

## Revisit when

Backups are designed (Phase 2), pgvector is needed (ADR-011), a
least-privilege role is added, or AETHER moves off the home PC.
