# ADR-025: Database backups and restore

**Status:** Proposed\
**Date:** 2026-11-16\
**Owner:** BoxDelta

## Context

All of AETHER's state is in one Postgres database in the Docker volume
`aether_pgdata` (ADR-011, ADR-020). Today that is the append-only `events`
table (metadata only) and `alembic_version`. Phase 2 adds Memory, the
first data that can't be rebuilt (ADR-024). There is no backup: a mistaken
`docker compose down -v`, a bad migration or a damaged volume loses
everything. #3 asks for a scheduled backup, a tested restore with row
counts compared, visible failures, and a decision on encryption and
location.

Constraints:

-   ADR-024 D5 (accepted): 7 daily plus 4 weekly backups, which is also
    how long a deleted memory record survives in backups (about 5 weeks).
-   ADR-024 §4: a restore brings back the past. Build step 16 adds the
    memory-specific restore steps, which read the pre-restore database.
-   Least privilege (#1, #12, ADR-020 addition): `aether_app` and its
    password are cluster-level, not part of a database dump.
-   The PC's disk is not encrypted (#57), and the owner URL already sits
    in `.env` (#60). A backup must not add another place where a password
    is passed or stored.
-   No cloud location without the owner's approval (docs/05 §11).

## Decision

-   **Method:** `pg_dump --format=custom` run **inside** the Postgres
    container (`docker exec`) as the owner, over the container's local
    socket. No password is used, so none appears on the host, in the
    process list, in the environment or in a log. Custom format is
    compressed, restores selectively, and `pg_restore --list` can verify
    it. Command: `uv run python -m core.backup` (`core/backup.py`).
-   **Safety of a run:** a private folder (700, files 600), a lock so
    runs never overlap, a temporary name, `fsync`, then verification
    (non-empty; `pg_restore --list` reads the whole archive and lists
    table data), and only then a rename to `aether-<UTC time>.dump`. A
    failed, empty or unreadable dump is deleted and never replaces or
    prunes a good one.
-   **Retention (D5):** the newest backup of each of the 7 most recent
    days that have one, plus the newest of each of the 4 most recent ISO
    weeks before the oldest daily one. With a daily backup, the oldest is
    4 to 5 weeks old. The newest backup is never pruned. Counting by days
    and weeks *that have backups* means a stopped schedule never ages
    the last backups out to nothing. Pruning runs only after a verified
    backup, and `--no-prune` skips it (used before a restore).
-   **Location:** `AETHER_BACKUP_DIR`, default
    `~/.local/share/aether/backups`. It must be absolute, outside the
    repository and private (the command refuses otherwise). It moves to
    the encrypted volume in #57. Never a cloud location.
-   **Schedule:** a systemd **user** timer, daily at 03:30 with
    `Persistent=true` (a missed run happens at the next login). The repo
    holds templates only (`scripts/systemd/`). The owner installs them by
    hand (runbook).
-   **Failure visibility:** exit code 1 and one JSON log line (`Backup
    failed`, the stage and exit code; no path beyond the file name, no
    URL, no password, no tool output). Under systemd it goes to the
    journal, and the unit shows as failed (`systemctl --user --failed`).
    **No event.** An event could be written without Core (the command
    could connect as the app role), but it would need a new event type
    and migration, load the app role's URL into a process that today
    needs no credential at all, and fail exactly when the database is
    the problem. The journal and the unit's failed state work even when
    Postgres is down.
-   **Restore:** always into a **new** database first
    (`aether_restore_check`), all or nothing (`--single-transaction
    --exit-on-error`). Then the database-level rights (not in a dump),
    row counts (events are append-only with growing ids, so the restored
    rows must equal the live rows up to the restored max id), and
    `scripts/restore_check.sql` (both triggers, the app role's rights,
    nothing for PUBLIC). Replacing the real database is a **rename**,
    keeping the old one as `aether_before_restore`, so going back is a
    rename too and nothing is dropped until the owner decides.
    Runbook: `docs/runbooks/db-backup-restore.md`.
-   **Roles:** recreated, not backed up. On the same cluster (the normal
    case) the role and its password are untouched by a restore. On a new
    cluster, one `CREATE ROLE aether_app LOGIN` before the restore lets
    the dump bring back its grants, and `core.db_admin set-app-password`
    takes the password from `.env`. Not chosen: `pg_dumpall
    --globals-only`, which would put every role's password verifier
    (including the superuser owner's) into each backup, for one role
    whose definition is already in migration 0003 and whose password is
    already in `.env`.
-   **Memory restore (build step 16):** not built here. This design
    doesn't block it: a dump holds every table, and the rename keeps the
    pre-restore database readable, which ADR-024 §4 needs to save the
    `memory.*` events written after the backup. Step 16 adds its steps
    between the check and the swap.

### Not encrypted at rest, yet

**Until #57 is done, backups are not encrypted at rest.** They are plain
files, private to the owner, on the same unencrypted disk. Today they
hold events only: metadata, no prompts, answers or memory. Real memory
data is gated on #57, #3 and #1/#12 (ADR-024 §7), so the folder must be
on the encrypted volume before then.

## Decisions for the owner

Each has a recommendation, the main alternative and the trade-off.

-   **D1. Method.** Recommended: `pg_dump` (custom format) inside the
    container. Alternatives: physical backups with WAL archiving
    (`pg_basebackup`, point-in-time recovery), or copying the volume.
    Trade-off: a logical dump is simple, portable across Postgres minor
    versions and verifiable, but loses everything after the last dump;
    WAL archiving loses almost nothing but is much more to run and test.
    A volume copy needs the database stopped.
-   **D2. Schedule.** Recommended: systemd user timer, daily 03:30,
    `Persistent=true`. Alternatives: n8n (ADR-013), cron, or a system
    timer. Trade-off: the user timer needs no root and no running n8n,
    and catches up after the PC was off; it runs only while your user
    session exists unless you enable lingering (`loginctl enable-linger`).
    Recommendation: no lingering for now (the PC is used daily).
-   **D3. Retention.** Recommended: 7 daily plus 4 weekly as above
    (ADR-024 D5), counted by days and weeks that have backups.
    Alternative: by calendar age (delete anything older than 35 days).
    Trade-off: calendar age guarantees a deletion is gone from backups
    after 35 days even if backups stop, but could leave no backup at
    all; counting keeps the last backups, and a deletion lingers
    longer only while backups are failing (which the journal shows).
-   **D4. Location.** Recommended: a private local folder
    (`~/.local/share/aether/backups`) that moves to the encrypted volume
    in #57; never a cloud location. Alternative: also an off-disk copy
    (an encrypted USB disk). Trade-off: the same disk protects against
    mistakes, not against losing the disk. Recommendation: add an
    off-disk encrypted copy after #57, as its own issue.
-   **D5. Roles.** Recommended: recreate `aether_app` (one `CREATE
    ROLE`) and set its password from `.env`. Alternative: back up
    globals with `pg_dumpall --globals-only`. Trade-off: globals restore
    the role in one step but put the owner's (superuser's) password
    verifier into every backup.
-   **D6. Failure visibility.** Recommended: exit code, one journal line
    and the unit's failed state. Alternatives: an event in the database,
    or a desktop notification (`OnFailure=` with `notify-send`).
    Trade-off: an event needs a migration and credentials and fails when
    the database is down; a notification is visible but one more unit.
-   **D7. Encrypting the files themselves before #57.** Recommended: no,
    rely on #57. Alternative: encrypt each dump to a public key (`age`
    or `gpg`) whose private key is kept off the PC. Trade-off: that
    protects backups even on the unencrypted disk and needs no secret on
    the PC, but a restore needs the offline key, and it adds a tool.
    Today the backups hold metadata only, and real memory data waits for
    #57 anyway.

## Consequences

-   Easier: one command makes a verified, private backup without any
    credential; restore is rehearsed in a throwaway database with one
    check that prints `ok`; a restore never destroys the current data.
-   Harder: the timer is installed by hand, and a user timer depends on
    the user session. Restoring onto a new cluster needs one manual
    `CREATE ROLE` and `set-app-password`.
-   Risks: backups sit on the same unencrypted disk as the database until
    #57; everything after the last backup is lost on restore; deleted
    memory survives in backups for up to about 5 weeks (D5), longer if
    backups stop.

## Revisit when

#57 lands (move the folder, confirm restore from it); Memory holds real
data; an off-disk copy is wanted; the database grows so that a daily full
dump is slow; or AETHER moves off the home PC.
