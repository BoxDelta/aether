# Database backups and restore (#3, ADR-025)

How AETHER's Postgres is backed up, how to install the daily timer, and how
to restore: first into a **new, throwaway database** that is checked, and
only then, if you really need it, in place of the real one.

Commands are fish, run from the repo root. Nothing here prints a password
or a URL. **Never run `docker compose down -v`**: it deletes the volume and
every record in it.

**What a backup holds today:** the `events` table (metadata only: IDs,
privacy level, route, error type, latency, token counts; never prompts or
answers) and `alembic_version`. Once Memory exists (Phase 2), backups also
hold memory records, including `personal` and `sensitive` ones.

**Not encrypted at rest yet.** Backups are plain files in a private
folder (mode 700, files 600) on the PC's disk, which is not encrypted. The
folder moves to the encrypted volume in #57 before any real memory data is
stored. Backups never go to a cloud location. They are on the **same
disk** as the database, so they protect against mistakes (a bad migration,
`down -v`, a corrupted volume), not against losing the disk (ADR-025 D4).

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

```fish
mkdir -p ~/.config/systemd/user
cp scripts/systemd/aether-backup.service scripts/systemd/aether-backup.timer ~/.config/systemd/user/
# If the repo is not at ~/Projects/aether, fix WorkingDirectory first:
grep WorkingDirectory ~/.config/systemd/user/aether-backup.service
systemctl --user daemon-reload
systemctl --user start aether-backup.service      # one run now
journalctl --user -u aether-backup.service -n 5 --no-pager   # expect "Backup completed"
systemctl --user enable --now aether-backup.timer
systemctl --user list-timers aether-backup.timer  # next run around 03:30
```

**What the timer runs.** It runs `core.backup` from the clone at
`WorkingDirectory`, as you, with your Docker access (which is root-level).
If that clone has an unreviewed branch checked out at 03:30, that branch's
code runs. Keep the clone on `main` when you're not working in it, or
point `WorkingDirectory` at a second clone that only ever pulls `main`
(ADR-025 D8, #63).

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
`systemctl --user daemon-reload`. The backups stay.

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
insert and read rights, that it can't update, delete, truncate or add
triggers, and that PUBLIC has no rights (`scripts/restore_check.sql`):

```fish
docker compose exec -T postgres psql -U aether -d aether_restore_check -At < scripts/restore_check.sql
```

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
> Once Memory exists, the ADR-024 §4 steps (re-delete, re-check levels,
> re-insert memory events, read from `aether_before_restore`) go between
> R4 and the swap. They're added to this runbook in build step 16 (#58).
> Until then this section is for events only.

1.  Do the drill (section 3, R1 to R4) and keep `aether_restore_check`;
    `restore_check.sql` must say `ok`.
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
