# Move an existing database to the least-privilege app role (#1, #12)

For an install that already has data in the `aether_pgdata` volume, at
migration `0002` (Core connects as the owner `aether`). After this, Core
connects as `aether_app`, which can only insert into and read `events`
(migration `0004` later adds read, insert, update and delete on
`memory_records`, #66).
The owner `aether` runs migrations only. Design: ADR-020, addition
2026-11-16. A fresh install follows the README instead.

**What this touches.** Migration `0003` creates the role `aether_app`,
grants it rights and revokes the default rights of PUBLIC. It does not
drop, recreate or rewrite any table, and it doesn't touch the volume:
existing events stay exactly as they are. `.env` gets one new line and one
changed line. Nothing here deletes data. **Never run `docker compose down
-v`**: it deletes the volume and every record in it.

**Who runs it.** The owner, because the steps edit `.env`. None of the
commands print a password or a URL. Don't paste `.env`, or the output of
`docker compose config`, anywhere.

All commands are fish, run from the repo root.

## 0. Before you start

-   The PR is merged and `main` is pulled: `git switch main; and git pull;
    and uv sync`.
-   Core is stopped (Ctrl+C in its terminal). Postgres is running:
    `docker compose ps` says `(healthy)`.
-   Note the commit you're on, for "Going back": `git rev-parse --short HEAD`.

Check the starting point. With the new code and no
`DATABASE_MIGRATION_URL` yet, Alembic uses `DATABASE_URL` (still the
owner):

```fish
uv run alembic current               # expect: 0002
docker compose exec postgres psql -U aether -d aether -c 'SELECT count(*), max(id) FROM events'
```

Write down the count and the max id; step 5 compares them.

## 1. Optional dump of the current database

A read-only copy, in case anything goes wrong. It holds events (metadata
only, no prompts or answers). The disk is not encrypted yet (#57), so
delete the file once step 5 has passed.

```fish
umask 077
docker compose exec -T postgres pg_dump -U aether -d aether -Fc > ~/aether-before-0003.dump
ls -l ~/aether-before-0003.dump      # non-zero size, -rw-------
```

## 2. Keep the owner URL for migrations

Copies today's `DATABASE_URL` (the owner) into a new
`DATABASE_MIGRATION_URL` line. Prints no URL.

```fish
umask 077                            # new files readable by you only
if grep -q '^DATABASE_MIGRATION_URL=.' .env
    echo "DATABASE_MIGRATION_URL is already set; not changed"
else
    string match -rv '^DATABASE_MIGRATION_URL=' < .env > .env.new
    and string replace -rf '^DATABASE_URL=' 'DATABASE_MIGRATION_URL=' < .env >> .env.new
    and chmod 600 .env.new
    and mv .env.new .env
    and echo "added DATABASE_MIGRATION_URL"
end
grep -c '^DATABASE_MIGRATION_URL=.' .env   # expect: 1
```

## 3. Run the migration (as the owner)

```fish
uv run alembic upgrade head
uv run alembic current               # expect: 0003 (head)
```

`aether_app` now exists with its rights but no password, so it can't log
in yet.

## 4. Give Core the app role

Points `DATABASE_URL` at `aether_app` with a new random password (same
host, port and database), then sets that password in Postgres. The
password is sent as a SCRAM verifier, never in plain text
(`core/db_admin.py`).

```fish
umask 077
set -l pw (openssl rand -hex 24)
string replace -r '^DATABASE_URL=postgresql\+asyncpg://[^@]*@' "DATABASE_URL=postgresql+asyncpg://aether_app:$pw@" < .env > .env.new
and chmod 600 .env.new
and mv .env.new .env
set -e pw
uv run python -m core.db_admin set-app-password
```

Expect: `ok: password set; Core can log in as aether_app@127.0.0.1:5432/aether`.
It changes only `aether_app`; if `DATABASE_URL` names any other role it
stops without changing anything. Use a generated password as above
(`openssl rand -hex 24`), not a short memorable one.

## 5. Check it worked

```fish
uv run python -m core.db             # expect: ok: connected to aether_app@127.0.0.1:5432/aether
docker compose exec postgres psql -U aether -d aether -c 'SELECT count(*), max(id) FROM events'
docker compose exec postgres psql -U aether -d aether -c '\dp events'
env AETHER_DB_TESTS=1 uv run pytest -m db
```

-   The count and max id are the same as in step 0 (Core was stopped).
-   `\dp events` shows `aether_app=ar/aether` (`a` insert, `r` select)
    and no line starting with `=` (PUBLIC).
-   The live tests pass. They prove that the app role can't update,
    delete or truncate events, drop or disable the triggers, or create,
    alter or drop tables, and that Core refuses the owner. They create a
    temporary login role for each test and drop it afterwards; rows they
    insert are rolled back.

Then start Core as usual and check `/ready`: `database` should be `ok`.
If Core refuses to start with "DATABASE_URL connects as a privileged
role", step 4 didn't change `DATABASE_URL`; run it again.

When everything passes, delete the dump from step 1:
`rm ~/aether-before-0003.dump`.

## Going back

Only if something is wrong. This returns to the single owner role. No
events are lost.

1.  Stop Core.
2.  Undo the migration, while the new code and both URLs are still in
    place, then drop the role:

    ```fish
    uv run alembic downgrade 0002
    uv run alembic current           # expect: 0002
    docker compose exec postgres psql -U aether -d aether -c 'DROP ROLE aether_app'
    ```

    If `DROP ROLE` says the role still has privileges, a leftover live
    test role may exist. List the roles with
    `docker compose exec postgres psql -U aether -d aether -c '\du'`,
    and drop any `aether_app_test_…` role the same way first.

3.  Point `DATABASE_URL` back at the owner and remove the migration line:

    ```fish
    umask 077
    set -l owner (string replace -rf '^DATABASE_MIGRATION_URL=' '' < .env)
    string match -rv '^DATABASE_(MIGRATION_)?URL=' < .env > .env.new
    and printf 'DATABASE_URL=%s\n' $owner >> .env.new
    and chmod 600 .env.new
    and mv .env.new .env
    set -e owner
    ```

4.  Check out the commit from step 0 (`git switch --detach <commit>`).
    The new code refuses to run Core as the owner, so it has to be the
    old code.
5.  `uv run python -m core.db` says `ok: connected to aether@…`, and
    the events count matches step 0.

If the database itself is damaged (not expected: no step rewrites data),
restore the dump from step 1 into a new database. That restore procedure
belongs to #3 and isn't covered here.
