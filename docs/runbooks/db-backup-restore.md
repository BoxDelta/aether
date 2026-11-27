# Database backups and restore (#3, ADR-025)

How AETHER's Postgres is backed up, how to install the daily timer, and how
to restore: first into a **new, throwaway database** that is checked, and
only then, if you really need it, in place of the real one.

Commands are fish, run from the repo root. Nothing here prints a password
or a URL. **Never run `docker compose down -v`**: it deletes the volume and
every record in it.

**What a backup holds today:** the `events` table (metadata only: IDs,
privacy level, route, error type, latency, token counts; never prompts or
answers), `alembic_version` and, since the memory API (ADR-024 step 6),
`memory_records`: full rows (content, key, level and provenance),
including `personal` and `sensitive` ones.

**Encrypted at rest only after the move (#57, ADR-026).** Until you've run
[`encrypted-volume.md`](encrypted-volume.md), backups are plain files in a
private folder (mode 700, files 600) on the PC's disk, which is not
encrypted. The move must be done before any real memory data is stored.

After the move:

-   Backups go to `~/.local/share/aether/vault/backups`, inside the same
    LUKS2 volume as the database.
-   The timer's drop-in refuses to run while that volume is locked: exit 1,
    one `err` journal line, the unit failed, and nothing written.
-   **Use that folder wherever this runbook says
    `~/.local/share/aether/backups`.**

Backups never go to a cloud location. They are on the **same disk** as the
database, so they protect against mistakes (a bad migration, `down -v`, a
corrupted volume), not against losing the disk (ADR-025 D4).

## 1. How a backup works

`uv run python -m core.backup` (`core/backup.py`):

-   runs `pg_dump --format=custom` **inside** the Postgres container, as
    the owner over the container's local socket, so no password is used
    or passed anywhere;
-   writes into `AETHER_BACKUP_DIR` (default
    `~/.local/share/aether/backups`, outside the repo) under a temporary
    name, checks it (`pg_restore --list` for the contents, then a full
    read of every data block, so a cut-off file fails), and only then
    renames it to `aether-<UTC time>.dump`;
-   prunes old backups: the newest of each of the last 7 days that have a
    backup, plus the newest of each of the 4 weeks before that (ADR-024
    D5). The newest backup is never pruned, and nothing is pruned after a
    failed run;
-   logs one JSON line: `Backup completed` with the file name, size and
    counts, or `Backup failed` with the stage and exit code (exit 1). A
    backup under half the size of the previous one adds a warning line.

"Completed" means the archive is complete and readable, not that it holds
the right data (an empty database backs up fine too). The restore drill
(section 3) is what proves the data.

Run one by hand:

```fish
uv run python -m core.backup
ls -l ~/.local/share/aether/backups/     # drwx------ folder, -rw------- files
```

## 2. Install the daily timer (by hand)

The repository holds templates only (`scripts/systemd/`). Nothing is
installed for you.

**The timer runs from a second clone, never your working clone** (ADR-025
D8). It runs as you, with your Docker access (which is root-level), so it
must only run merged code: a clone at `~/Projects/aether-backup` that only
ever has `main` checked out and has **no `.env`**. The backup needs no
password or URL. Without a `.env` it uses the defaults
(`AETHER_BACKUP_DIR` is `~/.local/share/aether/backups`, log level INFO).
The service refuses to start (its `ExecStartPre` line) unless that clone
is on `main` with no local changes. The manual commands in sections 3 and
4 still run from your working clone.

**2a. Create the second clone** (once). It comes from GitHub, so it only
ever gets what was merged:

```fish
git clone https://github.com/BoxDelta/aether.git ~/Projects/aether-backup
cd ~/Projects/aether-backup
git branch --show-current            # main
uv sync --frozen                     # its own virtual environment (.venv)
test -e .env; and echo "a .env is here: take it out"; or echo "no .env: good"
cd ~/Projects/aether
```

For a backup folder other than the default, don't add a `.env` to the
second clone. After step 2b's `cp`, add a line under `[Service]` in
`~/.config/systemd/user/aether-backup.service`:
`Environment=AETHER_BACKUP_DIR=/absolute/path`.

**2b. Install and start the timer:**

```fish
mkdir -p ~/.config/systemd/user
cp scripts/systemd/aether-backup.service scripts/systemd/aether-backup.timer ~/.config/systemd/user/
grep WorkingDirectory ~/.config/systemd/user/aether-backup.service   # %h/Projects/aether-backup
systemctl --user daemon-reload
systemctl --user start aether-backup.service      # one run now
journalctl --user -u aether-backup.service -n 5 --no-pager   # expect "Backup completed"
systemctl --user enable --now aether-backup.timer
systemctl --user list-timers aether-backup.timer  # next run around 03:30
```

**2c. Update the second clone, by hand.** It is never updated
automatically, so a stale clone keeps running the backup code it has.
Update it after merging a PR that changes `core/`, `pyproject.toml`,
`uv.lock` or `scripts/systemd/` (or simply after every merge). Never check
out another branch in it, and never work in it.

```fish
cd ~/Projects/aether-backup
git pull --ff-only                   # changes nothing if main has diverged
uv sync --frozen
cd ~/Projects/aether
systemctl --user start aether-backup.service      # check a run still works
journalctl --user -u aether-backup.service -n 3 --no-pager
```

If the merge changed the service or timer template, copy them again (the
`cp` line in 2b), then run `systemctl --user daemon-reload`.

If a run fails at `ExecStartPre`, the journal shows which check failed:

-   The git check: the second clone is not on a clean `main`. Check it with
    `git -C ~/Projects/aether-backup status`.
-   After the move to the encrypted volume, the line `aether-backup:
    encrypted volume not mounted (or no AETHER marker); no backup written`:
    run `aether-vault open`, then
    `systemctl --user start aether-backup.service`.

User timers run only while your user session exists. With
`Persistent=true`, a run missed while the PC was off or you were logged
out happens at the next login. To run without being logged in, enable
lingering (`loginctl enable-linger $USER`; ADR-025 D2).

**Seeing failures:**

```fish
systemctl --user --failed                         # aether-backup.service listed if the last run failed
journalctl --user -u aether-backup.service -p err --since -7d --no-pager
```

Remove the timer: `systemctl --user disable --now aether-backup.timer`,
then delete the two files from `~/.config/systemd/user/` and run
`systemctl --user daemon-reload`. The backups stay, and so does the
second clone until you delete it yourself.

## 3. Restore drill: into a new, throwaway database

Do this every so often, and always before step 4. It does **not** touch
the real `aether` database: it only creates and later drops
`aether_restore_check`.

**R1. Pick the backup.** Names sort by time, so the last one is the newest:

```fish
set backup (path sort ~/.local/share/aether/backups/aether-*.dump)[-1]
path basename $backup
```

**R2. Create the new database, with the database-level rights.** A dump
holds the tables, triggers and the grants on them, but not the rights on
the database itself (who may connect). Set them as migration 0003 does,
before any data goes in:

```fish
docker compose exec -T postgres createdb -U aether aether_restore_check
docker compose exec -T postgres psql -U aether -d postgres -c 'REVOKE ALL ON DATABASE aether_restore_check FROM PUBLIC'
docker compose exec -T postgres psql -U aether -d postgres -c 'GRANT CONNECT ON DATABASE aether_restore_check TO aether_app'
```

**R3. Restore into it.** All or nothing: on any error, nothing is left
half-restored. If it fails on a `GRANT` naming `aether_app`, the role is
missing (section 5). A restore runs the dump's SQL as the superuser
`aether`, which is safe only because the backup folder is private to you:
never restore a dump from anywhere else.

```fish
docker compose exec -T postgres pg_restore -U aether -d aether_restore_check --exit-on-error --single-transaction < $backup
```

**R4. Check it.** Rows: events are append-only and their `id` only grows,
so the restored events must be exactly the live events up to the
restored max id. The two numbers printed last must be equal.

```fish
docker compose exec -T postgres psql -U aether -d aether_restore_check -c 'SELECT version_num FROM alembic_version'
set max (docker compose exec -T postgres psql -U aether -d aether_restore_check -Atc 'SELECT coalesce(max(id), 0) FROM events')
docker compose exec -T postgres psql -U aether -d aether_restore_check -Atc 'SELECT count(*) FROM events'
echo "SELECT count(*) FROM events WHERE id <= $max" | docker compose exec -T postgres psql -U aether -d aether -At
```

Triggers and rights: this prints exactly `ok` (else `NOT OK:` and the
failing checks). It checks both append-only triggers, the app role's
insert and read rights on `events`, that it can't update, delete,
truncate or add triggers there, its read, insert, update and delete
rights on `memory_records` (and no truncate, triggers or references),
and that PUBLIC has no rights (`scripts/restore_check.sql`):

```fish
docker compose exec -T postgres psql -U aether -d aether_restore_check -At < scripts/restore_check.sql
```

A backup taken before migration `0004` (the `version_num` above is
`0003`) has no `memory_records` table, so the check prints
`NOT OK: memory_table` and nothing else. That is expected for such a
backup. If you replace the database with it (section 4), run
`uv run alembic upgrade head` right after the swap (step 4), before the
checks in step 5; the check then says `ok`. Any other failing name is a
real problem: don't swap.

**R5. End of a drill.** Drop the throwaway database. This only removes
`aether_restore_check`; check the name before you press Enter.

```fish
docker compose exec -T postgres dropdb -U aether aether_restore_check
```

## 4. Replace the real database with the restored one

> **Warning.** This replaces what Core sees with the backup. Everything
> written after the backup is gone from the live database: events now,
> and with Memory, also new records, deletions, corrections and level
> changes. The old database is **kept** under another name, so you can go
> back, and nothing is dropped in this section.
>
> Memory records exist since the memory API (ADR-024 step 6). The ADR-024
> §4 steps (re-delete, re-check levels, re-insert memory events, read
> from `aether_before_restore`) go between R4 and the swap; they're added
> to this runbook in build step 16 (#58). Until then a swap brings back
> memory records deleted after the backup and undoes later corrections
> and level changes, with nothing to repair it: a record raised to
> `sensitive` goes back to its lower level, so once retrieval exists it
> could be sent at that level. That is acceptable only while memory holds
> invented data: real data waits for #57 and these restore steps (step 16).

1.  Do the drill (section 3, R1 to R4) and keep `aether_restore_check`;
    `restore_check.sql` must say `ok` (or only `NOT OK: memory_table` for
    a backup from before migration `0004`; see R4).
2.  Stop Core (Ctrl+C) and the timer:
    `systemctl --user stop aether-backup.timer`.
3.  If the live database is still readable, back it up first, with a
    label. A labelled backup (`aether-<time>-prerestore.dump`) is outside
    retention: this run prunes nothing (so the backup you restore from
    stays), and later runs never prune or count it.

    ```fish
    uv run python -m core.backup --label prerestore
    ```
4.  Swap the names. The current database becomes `aether_before_restore`.

    ```fish
    docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether RENAME TO aether_before_restore'
    docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether_restore_check RENAME TO aether'
    ```

    If a rename says the database is in use, something is still
    connected (Core, a `psql`): stop it and run the line again.

5.  Check:

    ```fish
    uv run alembic current                # the restored revision, (head)
    uv run python -m core.db              # ok: connected to aether_app@…
    docker compose exec -T postgres psql -U aether -d aether -At < scripts/restore_check.sql   # ok
    ```

    Then start Core, check `/ready`, and start the timer again:
    `systemctl --user start aether-backup.timer`.

6.  Keep `aether_before_restore` until you're sure, then drop it.
    **Dropping it deletes the pre-restore data for good**, except for the
    labelled backup from step 3, which holds the same data.

    ```fish
    docker compose exec -T postgres dropdb -U aether aether_before_restore
    ```

7.  **Delete the labelled backups by hand**, within 5 weeks. Retention
    never touches them, so without this, anything deleted before the
    restore (with Memory: deleted records) would survive in them for
    good, breaking ADR-024 D5. The same applies to
    `aether_before_restore` in step 6.

    ```fish
    ls ~/.local/share/aether/backups/*-*-*.dump     # labelled backups only
    rm ~/.local/share/aether/backups/aether-*-prerestore.dump
    ```

### Going back (before step 6)

Swap the names back. The restored copy is kept as
`aether_restore_rejected`; drop it when you no longer need it.

```fish
docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether RENAME TO aether_restore_rejected'
docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether_before_restore RENAME TO aether'
uv run python -m core.db              # ok: connected to aether_app@…
```

## 5. If the whole cluster is gone (volume lost)

Roles are cluster-level and are **not** in a database dump: not
`aether_app`, and not its password. The grants on the tables *are* in the
dump, and they need the role to exist. So the role is recreated first,
the dump brings back its grants, and its password comes from `.env`
(ADR-025 D5; why not a `pg_dumpall --globals-only` backup is explained
there).

1.  Start a fresh container. It creates a new empty volume, the owner
    `aether` with `POSTGRES_PASSWORD` from `.env`, and an empty database
    `aether`.

    ```fish
    docker compose up -d
    docker compose ps                     # (healthy)
    ```

2.  Recreate the app role. It has no special attributes and no password
    yet, like migration 0003 creates it.

    ```fish
    docker compose exec -T postgres psql -U aether -d postgres -c 'CREATE ROLE aether_app LOGIN'
    ```

3.  Do the drill, section 3 R1 to R4 (the row comparison against the live
    database doesn't apply: it is empty).
4.  Put the restored database in place of the empty one, then drop the
    empty one (it holds nothing):

    ```fish
    docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether RENAME TO aether_empty'
    docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether_restore_check RENAME TO aether'
    docker compose exec -T postgres dropdb -U aether aether_empty
    ```

5.  Give the app role its password from `.env` and check:

    ```fish
    uv run python -m core.db_admin set-app-password   # ok: password set; …
    uv run python -m core.db                          # ok: connected to aether_app@…
    uv run alembic current                            # (head)
    docker compose exec -T postgres psql -U aether -d aether -At < scripts/restore_check.sql   # ok
    ```

Everything written after that backup is lost. With Memory, so are
deletions, corrections and level changes made after it, and the runbook
in build step 16 says what that means for each record.
