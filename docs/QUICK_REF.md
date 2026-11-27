# Quick reference

Short map for assistants and new contributors. Reasons behind the design
are in `docs/adr/`; a longer tour is in `docs/CODE_TOUR.md`.

## Entry points

-   Core app: `core/app.py` (`create_app`, an app factory).
-   Run it (fish): `uv run uvicorn core.app:create_app --factory --reload`
-   Local Postgres: `docker compose up -d` (see `docker-compose.yml`).
-   Migrations: `uv run alembic upgrade head` (`migrations/versions/`).

## Key modules

-   `core/`: `app.py` factory, `config.py` settings, `auth.py` bearer
    token, `chat.py` `/chat`, `ready.py` `/ready`, `memory_api.py`
    `/memory` (list, get, search, owner create, approve, reject, correct,
    delete, export), `db.py` and
    `db_admin.py` Postgres, `events.py` append-only event log, `log.py`
    logging, redaction and `SecretCheck`, `identity.py` identity loader,
    `backup.py` verified backups.
-   `router/` (Nexus): `contract.py` provider contract and privacy
    levels, `routing.py` rules and failover, `identity_call.py` the one
    function every model call goes through, `ollama.py` (local),
    `claude.py` (cloud), `hardware.py` GPU/RAM probe,
    `memory_context.py` the memory a call gets and its backstop,
    `echo.py` the echo backstop.
-   `memory/`: `store.py` the memory store, `retrieval.py` which memory
    one model call may get, `types.py` its types.
-   `identity/aether.yaml`: the single AETHER identity.
-   `dispatch/`: dispatcher prototype (ADR-030, dev tooling, not Core):
    `config.py`, `github.py` (read-only), `rules.py` (always-Claude list),
    `classify.py`, `gpu.py`, `notify.py` (Discord), `__main__.py`. See `dispatch/README.md`.
-   `scripts/`: `safemerge.fish`, `identity_continuity.py`, systemd units
    (backup, dispatcher triage).

## Tests

-   Live in `tests/`, one `test_<module>.py` per area; shared helpers in
    `conftest.py`, `fakes.py`, `chat_helpers.py`, `fake_secrets.py`.
-   Tests marked `db` need the local Postgres and `AETHER_DB_TESTS=1`.

## Commands (same as Sentinel, run from the repo root)

-   Sync deps: `uv sync`
-   Tests: `uv run pytest -W error::DeprecationWarning`
-   Tests with coverage: `uv run pytest -W error::DeprecationWarning --cov --cov-report=term-missing`
-   DB tests (fish): `env AETHER_DB_TESTS=1 uv run pytest -m db -W error::DeprecationWarning`
-   Lint: `uv run ruff check .`
-   Format check: `uv run ruff format --check .`
-   Type check (mypy, strict): `uv run mypy`
-   Local hooks: `uv run pre-commit run --all-files`

## Rules that bite

-   Secrets only in `.env` (never read it); new variable names go in
    `.env.example`.
-   One branch and one PR per issue; never push to `main`.
