# AETHER

One identity. Many minds. Shared memory.

AETHER is a personal AI platform that gives you one assistant across many
models. Instead of treating each AI provider as a separate assistant with
its own context, AETHER puts a single identity, user-controlled memory and
a model router in front of them. It can answer from a local model (Ollama)
or a cloud model (Claude), and it picks between them using privacy level,
availability and available GPU memory. Requests labelled Secret are never
sent to any model, and cloud models only receive Public or Personal
requests (cloud is opt-in per request). Labels are set by the caller, and
unlabelled requests are treated as Personal; AETHER does not yet classify
content itself.

Status: early-stage and built by one person on Arch Linux (GNOME).
Phases 0 and 1 are done: Core (the API), model routing and the identity
layer work today. Phase 2 (shared memory) is in progress: the memory
store and its owner API (`/memory`: list, search, create, approve,
correct, delete and export) exist, and approved memory goes into model
calls within each provider's privacy limit. A web UI
comes in Phase 3. Phase status lines are in
[`docs/04_Development_Roadmap.md`](docs/04_Development_Roadmap.md). It is
not production-ready, and it is built for a single user on their own
machine.

Start with [`docs/README.md`](docs/README.md) for the documentation map
and [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to contribute.
[`CLAUDE.md`](CLAUDE.md) holds the rules for AI coding assistants.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) (ADR-019). The Python version is pinned in
`.python-version` and uv installs it if missing. On Arch / GNOME:

```fish
sudo pacman -S uv
```

Create the virtual environment from the lockfile:

```fish
uv sync
```

Run the checks (the same ones Sentinel runs):

```fish
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Coverage report for `core/` and `router/` (branch coverage, missing lines
listed). It is report-only for now: there is no minimum, and Sentinel
writes the same table to the `checks` job summary.

```fish
uv run pytest --cov --cov-report=term-missing
```

Install the local guardrails once per clone (pre-commit hooks, #42):

```fish
uv run pre-commit install
```

Every `git commit` then runs gitleaks on the staged changes, ruff (the
version from `uv.lock`, same scope as Sentinel; it fixes what it can and
stops the commit so you can review and `git add -u`), and checks for private
keys, merge-conflict markers and files over 500 KB. The first run builds
gitleaks with Go (pre-commit downloads Go itself; about a minute). Run all
hooks by hand, or update the pinned hook versions (Dependabot also
proposes these weekly):

```fish
uv run pre-commit run --all-files
uv run pre-commit autoupdate --freeze
```

`git commit --no-verify` skips the hooks; Sentinel still runs every check
on the pull request. `.claude/settings.json` holds Claude Code permission
rules for this repo (see `docs/05` §10.3).

### Merging with `safemerge` (backup copy in `scripts/`)

Until rulesets are enabled after the repo goes public (ADR-018, ADR-022),
GitHub Free doesn't enforce branch protection on this repo, so
the owner merges only with the `safemerge` fish function. That function is
the merge gate. `scripts/safemerge.fish` is **a backup and documentation
copy only**:

-   The live function lives in the owner's fish config
    (`~/.config/fish/functions/`). **Never source or symlink it; updating
    the live function is a deliberate manual step by the owner after
    reading the diff.** The merge gate must not be changeable through a
    pull request it gates.
-   Changing the repo copy prompts for approval in Claude Code
    (`.claude/settings.json`). That covers Claude Code's edit tools only,
    not shell commands, so the real protection is that the live function
    stays outside the repo.
-   From time to time, compare the live function with the backup:

    ```fish
    diff (functions safemerge | tail -n +2 | psub) scripts/safemerge.fish
    ```

    `tail -n +2` drops the `# Defined in …` line that `functions` adds. No
    output means they match. Anything else means one of them changed.

Usage: `safemerge <PR number> [reviewed commit sha]`. It:

1.  Prints usage and stops if no PR number is given.
2.  Reads the PR's head commit (`gh pr view`). It stops if that fails or
    the result isn't a full 40-character lowercase commit hash.
3.  If a reviewed commit is given, it must be 7–40 lowercase hex
    characters, so wildcards like `*` and one-character prefixes are
    refused. It stops unless the head starts with exactly those
    characters, so a push after your review is never merged unseen.
4.  Waits for every check on the PR (`gh pr checks --watch --fail-fast`)
    and stops on the first failure. With no checks reported at all,
    `gh` (2.104.0) exits 1 ("no checks reported"), so it stops then too.
5.  Merges with a merge commit and deletes the branch, using
    `--match-head-commit <head>`: GitHub refuses the merge if the head
    moved since step 2.
6.  If run inside the aether checkout (`origin` is `BoxDelta/aether`), it
    switches to `main` and pulls. Elsewhere it prints that reminder
    instead.

`tests/test_safemerge.py` checks the syntax (`fish -n`) and this logic
with `gh` and `git` stubbed. It never merges, and is skipped where fish
isn't installed (Sentinel).

Run Core locally (http://127.0.0.1:8000/health):

```fish
uv run uvicorn core.app:create_app --factory --reload
```

Every route except `/health` needs the API token (ADR-021). Generate one
and put it in `.env` as `AETHER_API_TOKEN=` (at least 32 characters).
Without it, Core serves only `/health` and answers 503 everywhere else.

```fish
openssl rand -hex 32   # copy the output into .env: AETHER_API_TOKEN=<output>
```

Send a chat request. Privacy defaults to `personal` and `allow_cloud` to
`false`. Requests are limited to 256 KB, 50 messages, 20,000 characters per
message and 60,000 in total.

Cloud answers are capped at `AETHER_CLOUD_MAX_OUTPUT_TOKENS` output tokens
(default 4096, thinking included). The setting applies to cloud calls only;
local models aren't capped by it. A request may lower the limit with
`"max_tokens": N` but not raise it (422). An invalid value in `.env` stops
Core at startup. A cut-off answer comes back with `"finish_reason": "length"`,
sometimes with empty text if thinking used the whole budget.

Approved memory is added to each model call within that provider's limit:
Public and Personal on a trusted Ollama host (loopback, a private address,
or any host with `AETHER_OLLAMA_TRUST_HOST=true`), Public only for Claude
and for an untrusted host. Sensitive memory is sent only to a trusted
Ollama host, and only when the request sets `"include_sensitive": true`.

```fish
set -l token (string replace -r '^AETHER_API_TOKEN=' '' (grep '^AETHER_API_TOKEN=' .env))
curl -s http://127.0.0.1:8000/chat -H "authorization: Bearer $token" \
    -H 'content-type: application/json' \
    -d '{"messages": [{"role": "user", "content": "Hello"}]}' | jq
```

Check whether Core can answer: `/ready` reports one status word per
component (`ollama`, `claude`, `database`) and an overall `ready`,
`degraded` or `not_ready` (503). See docs/02 for the meanings. It needs
the token; `/health` doesn't.

```fish
curl -s http://127.0.0.1:8000/ready -H "authorization: Bearer $token" | jq
```

Errors come back as `{"request_id", "error": {"type", "message", "attempts"}}`:
`502 RouteFailed` (providers failed; each attempt has provider, error type
and a scrubbed message), `503 NoRoute` (no provider may take it, e.g. Secret
data), `413` (body too large), `422` (invalid request, input not echoed;
or `SecretContentRefused`: the text looks like a secret, so it was not sent
to the cloud),
`401` (missing or wrong token), `503 AuthNotConfigured` (no valid token in
`.env`). If `DATABASE_URL` is set, each request records
metadata-only events after the response is sent. If the event write fails,
the answer is unaffected and one warning is logged.

### Local inference (Ollama)

Local inference uses [Ollama](https://ollama.com). It's optional for
development (tests mock it), but needed to route to the local model. On
Arch / GNOME with an NVIDIA GPU:

```fish
sudo pacman -S ollama-cuda
sudo systemctl enable --now ollama
ollama pull llama3.1:8b   # or whatever OLLAMA_MODEL is set to
```

**On an 8 GB GPU, turn on flash attention and a q8_0 KV cache.** With
Ollama's defaults, `llama3.1:8b` leaves about 1.4 GB of VRAM free, below
the 1.5 GB desktop reserve (Charter). With these two settings it leaves
about 1.7 GB, measured in the Phase 1 exit check
(`docs/runbooks/phase1-exit-check.md`).

Put them in a systemd drop-in so package updates keep them:

```fish
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_FLASH_ATTENTION=1"\nEnvironment="OLLAMA_KV_CACHE_TYPE=q8_0"\n' | sudo tee /etc/systemd/system/ollama.service.d/aether-kv.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
systemctl show ollama -p Environment   # expect both variables listed
```

Other programs that hold VRAM count against the same budget. A
text-to-speech server such as Chatterbox uses about 5 GB, and the 8B model
doesn't fit beside it.

Logs are JSON lines on stdout, one per event, with `request_id` and
`session_id`. `LOG_LEVEL` sets the level. Configured secrets and anything
shaped like an Anthropic key are replaced with `[REDACTED]`. Every model
call logs one `Model call` record with the identity version (no prompt or
answer text). Third-party HTTP/SDK loggers stay at `WARNING` even with
`LOG_LEVEL=DEBUG`, because the Anthropic SDK logs full request bodies at
debug level. To read them
in a terminal (fish):

```fish
uv run uvicorn core.app:create_app --factory | jq -c '{level, logger, request_id, message}'
```

Copy `.env.example` to `.env` and fill in values locally. Never commit `.env`.

## Database (Postgres)

One Postgres in Docker holds memory, runtime tasks and events (ADR-011,
ADR-020). It listens on `127.0.0.1:5432` only. Its data lives in the Docker
volume `aether_pgdata`, not in this repository.

Two roles (ADR-020, #1, #12). The owner `aether` runs migrations only
(`DATABASE_MIGRATION_URL`). Core connects as `aether_app`
(`DATABASE_URL`), which can insert and read events but can't change or
delete them, and Core refuses to start as the owner. On the
`memory_records` table (ADR-024) it can also update and
delete, but never truncate, add triggers, alter or drop. An install from
before this change moves over with
[`docs/runbooks/db-app-role.md`](docs/runbooks/db-app-role.md).

Create the passwords once. This appends three lines to `.env` and prints
nothing secret. It does nothing if `.env` already has a non-empty value.
If `.env` was copied from `.env.example`, delete its empty
`POSTGRES_PASSWORD=`, `DATABASE_MIGRATION_URL=` and `DATABASE_URL=` lines
first.

```fish
if grep -qE '^(POSTGRES_PASSWORD|DATABASE_MIGRATION_URL|DATABASE_URL)=.' .env 2>/dev/null
    echo ".env already has database settings; not changed"
else
    set -l owner (openssl rand -hex 24)
    set -l app (openssl rand -hex 24)
    printf 'POSTGRES_PASSWORD=%s\nDATABASE_MIGRATION_URL=postgresql+asyncpg://aether:%s@127.0.0.1:5432/aether\nDATABASE_URL=postgresql+asyncpg://aether_app:%s@127.0.0.1:5432/aether\n' $owner $owner $app >> .env
    echo "added POSTGRES_PASSWORD, DATABASE_MIGRATION_URL and DATABASE_URL to .env"
end
```

Start it, apply migrations (as the owner), set the app role's password,
and check it:

```fish
docker compose up -d                # first run creates the volume and sets the owner password
docker compose ps                   # STATUS should say (healthy)
uv run alembic upgrade head         # creates the tables and the aether_app role
uv run python -m core.db_admin set-app-password   # "ok: password set; Core can log in as aether_app@…"
uv run python -m core.db            # "ok: connected to aether_app@127.0.0.1:5432/aether"
env AETHER_DB_TESTS=1 uv run pytest -m db   # optional live checks (rows rolled back)
```

`set-app-password` sends Postgres a SCRAM verifier, never the password
itself. Run it again after changing the app password in `DATABASE_URL`.

With `AETHER_DB_TESTS=1`, a missing `DATABASE_URL` or an unmigrated
database fails the live tests instead of skipping them. Sentinel runs them
on every push and pull request (#52). It starts this same `docker-compose.yml`
service on the throwaway CI runner, with a password generated for that run
and masked in the logs. Dependabot proposes digest and 18.x updates for
the image; major versions are ignored, because Postgres 19 needs an upgrade
plan.

What the app reports (`uv run python -m core.db`, exit code 1):

| Situation | Message starts with |
|---|---|
| `DATABASE_URL` empty | `not_configured: DATABASE_URL is not set` |
| Malformed URL | `misconfigured: DATABASE_URL must start with postgresql+asyncpg://` (never echoes the URL) |
| Password under 8 characters | `misconfigured: DATABASE_URL needs a password of at least 8 characters` |
| `DATABASE_URL` is the owner | `misconfigured: DATABASE_URL connects as a privileged role …` (Core also refuses to start) |
| Container stopped / wrong port | `unreachable: cannot reach Postgres at aether@127.0.0.1:5432/aether. Is the container running?` |
| Wrong password | `auth_failed: Postgres rejected the login for aether_app@127.0.0.1:5432/aether. For the app role, set its password …` |
| No answer within 5 s | `timeout: no answer from Postgres at …` |

Stop and start without losing data:

```fish
docker compose stop        # data stays in the volume
docker compose start
docker compose down        # removes the container; the volume and data stay
```

`docker compose down -v` **deletes the volume and every record in it.**
`docker compose config` prints the resolved password, so don't paste its
output anywhere.

Postgres stores the owner password only when the volume is first created.
Changing `POSTGRES_PASSWORD` in `.env` later does not change it, and
migrations then fail with `auth_failed`. To change the owner password, set
it in the database (this asks for the new one twice), then update
`POSTGRES_PASSWORD` and `DATABASE_MIGRATION_URL` in `.env`. To change the
app password, edit `DATABASE_URL` and run `uv run python -m core.db_admin
set-app-password`.

```fish
docker compose exec postgres psql -U aether -d aether -c '\password aether'
```

### Backups

`uv run python -m core.backup` makes a verified backup (`pg_dump` inside the
container, no password used) in `AETHER_BACKUP_DIR` (default
`~/.local/share/aether/backups`, private, outside the repo) and keeps 7
daily plus 4 weekly backups (ADR-025). A daily systemd user timer is
provided as templates in `scripts/systemd/`, installed by hand, and runs
from a second clone that only ever has `main` checked out
(`~/Projects/aether-backup`, no `.env`), never from the working clone.
Install steps, the restore drill into a throwaway database, replacing the
real database and going back are in
[`docs/runbooks/db-backup-restore.md`](docs/runbooks/db-backup-restore.md).

-   Backups are **not encrypted at rest** until #57, and sit on the same
    disk as the database. Since the memory API (`POST /memory`) they
    hold full memory records too (content, key, level and provenance),
    in plain text. Until #57 and the restore steps of build step 16
    (#58) are done, store only invented data in memory.
-   A deleted record stays in backups until the last backup taken before
    the delete is pruned: about 5 weeks with daily backups.
-   Roles aren't in a backup; a restore onto a new cluster recreates
    `aether_app` and sets its password from `.env` (runbook, section 5).

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). The AETHER
name is not licensed.
