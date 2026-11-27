# Encrypted volume for the database and backups (#57, ADR-026)

This runbook moves AETHER's Postgres data and its backups into one LUKS2
container file of 20 GB, unlocked with a passphrase only you know. You run
every step yourself; nothing in the repo does it for you. The design and its
reasons are in ADR-026.

-   **Do it before real memory data is stored.** Anything ever written
    unencrypted can't be reliably wiped from this disk afterwards (ADR-026
    §7). Stages 0–5 were done on 2026-11-27 (results on #57); at the move
    the database held event metadata and 0 memory records. Stage 7 (#152)
    is still open: until then, a compose run without the override starts
    the old unencrypted database, so keep `COMPOSE_FILE` in `.env`. If
    real memory text was ever written there, treat it as exposed on this
    disk.
-   Commands are fish, run from the repo root (`~/Projects/aether`) unless a
    step says otherwise.
-   Lines marked `# expect:` show what you should see. Anything else: stop,
    and use that stage's rollback.
-   **Never run `docker compose down -v`.** It deletes the volume and every
    record in it.
-   Nothing here prints or stores the passphrase. It is typed only at
    cryptsetup's own prompt.

The paths are fixed. The helper script and the compose override expect
exactly these:

-   `~/.local/share/aether/store/`: a btrfs subvolume, left out of snapshots,
    holding the container file `aether-vault.luks`.
-   `~/.local/share/aether/vault/`: the mount point. It is empty and
    immutable while locked. Once mounted it holds `data/` (Postgres),
    `backups/` and the marker `.aether-vault`, plus `data/.aether-vault`.
-   `/dev/mapper/aether-vault`: the unlocked device.

Stages:

-   0: checks.
-   1: the file.
-   2: encrypt and format.
-   3: passphrase and header backup.
-   4: move the data.
-   5: switch over.
-   6: daily routine.
-   7: clean up the old data, later and only with your approval.

## Stage 0. Checks (changes nothing)

```fish
stat -f -c %T ~/.local/share                      # expect: btrfs
df -BG --output=avail ~/.local/share | tail -1     # expect: at least 30G
```

The file takes 20 GB at once (fully allocated), plus margin for the host. If
fewer than 30G are free, stop and free space first.

```fish
swapon --show                  # expect: only /dev/zram0 (RAM), or nothing
findmnt -n -o FSTYPE /tmp      # expect: tmpfs
cat /sys/power/state; cat /sys/power/disk 2>/dev/null
```

-   If `swapon` lists a disk partition or a swap file, memory pages can
    reach the unencrypted disk. Use zram or encrypted swap, or accept it
    knowingly (ADR-026 §8).
-   Don't hibernate the PC once memory holds real data: hibernation writes
    RAM to disk.

```fish
snapper list-configs 2>/dev/null; ls /timeshift 2>/dev/null
```

If home or `/var/lib/docker` is snapshotted, snapshots taken before the move
keep copies of the old, unencrypted data until the tool's retention removes
them (stage 7).

Write down the current numbers. You'll compare against them after the move:

```fish
docker compose exec -T postgres psql -U aether -d aether -Atc 'SELECT count(*), max(id) FROM events'
# expect: two numbers, e.g. 14|44 (more if Core ran since). Note both.
docker compose exec -T postgres psql -U aether -d aether -Atc 'SELECT count(*) FROM memory_records'
# expect: one number (invented test records only). Note it.
uv run alembic current          # expect: … (head). If not: uv run alembic upgrade head
```

## Stage 1. The container file (no sudo)

```fish
mkdir -p ~/.local/share/aether; chmod 700 ~/.local/share/aether
btrfs subvolume create ~/.local/share/aether/store
# expect: Create subvolume '/home/<you>/.local/share/aether/store'
```

If that says `Permission denied`, run it with `sudo` and then
`sudo chown $USER: ~/.local/share/aether/store`.

```fish
stat -c %i ~/.local/share/aether/store             # expect: 256 (a subvolume root)
chmod 700 ~/.local/share/aether/store
chattr +C ~/.local/share/aether/store              # new files in it are NOCOW
set f ~/.local/share/aether/store/aether-vault.luks
test -e $f; and echo "STOP: $f already exists"; or echo "ok: no file yet"
touch $f; chmod 600 $f
chattr +C $f                                       # NOCOW, while the file is still empty
lsattr $f                                          # expect: a C among the flags
fallocate -l 20G $f
du -h --apparent-size $f; du -h $f                 # expect: 20G and 20G (not sparse)
```

-   Why NOCOW: copy-on-write would fragment a file that gets small random
    writes.
-   Why fully allocated: a sparse file could hit "disk full" in the middle
    of a Postgres write.
-   Why its own subvolume: snapshots of home leave it out.
-   Details are in ADR-026 §2.

**Rollback (stage 1):**

```fish
rm ~/.local/share/aether/store/aether-vault.luks
btrfs subvolume delete ~/.local/share/aether/store    # or with sudo
```

## Stage 2. Encrypt, format, prepare the mount point

**2a. Format the file as LUKS2.** This asks you to type `YES`, then your
passphrase twice. Choose a long passphrase (several random words), and save
it in your password manager **before** you go on.

```fish
sudo cryptsetup luksFormat --type luks2 $f
sudo cryptsetup luksDump $f | string match -r '^(?:Version|\s+cipher|\s+PBKDF):.*'
# expect: Version: 2, cipher: aes-xts-plain64, PBKDF: argon2id
```

**2b. Open it** (this asks for the passphrase):

```fish
sudo cryptsetup open --type luks2 $f aether-vault
```

Optional, and only here, before `mkfs.ext4`: fill the volume with
random-looking data, so nobody with the disk can see which parts are used
(ADR-026 §2). Zeros written through the open volume land on disk as
ciphertext. It writes 20 GB, and "No space left on device" at the end is
expected:

```fish
sudo dd if=/dev/zero of=/dev/mapper/aether-vault bs=4M status=progress
```

Then make the filesystem:

```fish
sudo mkfs.ext4 -L aether-vault -m 0 /dev/mapper/aether-vault
# expect: "Writing superblocks and filesystem accounting information: done"
```

**2c. The mount point: empty, private, immutable.** Nothing can be written
into it while the volume is not mounted, not even by root or Docker.

```fish
set m ~/.local/share/aether/vault
mkdir -m 700 $m
sudo chattr +i $m
lsattr -d $m                                       # expect: an i among the flags
touch $m/test                                      # expect: Operation not permitted
```

**2d. First mount, then the layout and the two markers:**

```fish
sudo mount -o nodev,nosuid,noexec /dev/mapper/aether-vault $m
sudo chown $USER: $m; chmod 700 $m
mkdir -m 711 $m/data            # 711: the container's postgres user must pass through
mkdir -m 700 $m/backups
touch $m/.aether-vault $m/data/.aether-vault
chmod 600 $m/.aether-vault $m/data/.aether-vault
findmnt -n -o SOURCE,FSTYPE --target $m            # expect: /dev/mapper/aether-vault ext4
ls -la $m                                          # .aether-vault, backups, data, lost+found
```

**2e. Lock it again, and check that the bare mount point stays empty:**

```fish
sudo umount $m; and sudo cryptsetup close aether-vault
ls -A $m                                           # expect: nothing
test -e /dev/mapper/aether-vault; and echo "still open"; or echo "locked"   # expect: locked
```

**Rollback (stage 2):** lock it as in 2e, then
`sudo chattr -i $m; and rmdir $m`, then stage 1's rollback.

## Stage 3. Passphrase and LUKS header backup

-   **Passphrase:** keep it in your password manager, one that still works
    if this PC's disk dies (synced, or with an offline copy). Never in
    `.env`, a file on this disk, a script or a note on the desktop.
-   **If you forget it, the database AND every backup are gone.** Both are
    inside the volume. Nobody can recover them.
-   Optional: a second passphrase on paper, kept somewhere safe (a second
    key slot):

    ```fish
    sudo cryptsetup luksAddKey $f       # asks for an existing passphrase, then the new one twice
    ```

**Header backup.** If the header (the first 16 MiB) is damaged, even the
right passphrase can't open the volume. Back it up to RAM
(`/run/user/<uid>`), copy it off this machine, then remove it here:

```fish
set h /run/user/(id -u)/aether-vault-header.img
sudo cryptsetup luksHeaderBackup $f --header-backup-file $h
sudo chown $USER: $h
ls -l $h                                           # expect: about 16M
```

1.  Copy `$h` off this machine. Use a password-manager attachment next to
    the passphrase, or a USB stick you keep elsewhere.
2.  Then remove the local copy:

```fish
rm $h
```

What the header backup exposes: whoever has it, a copy of the container
file **and** a passphrase that was valid when you took the backup can open
the volume, even after you change that passphrase. So:

-   after changing or removing a passphrase, take a new header backup;
-   destroy the old copies.

On its own, without the container file, it can't decrypt any data. But it
does hold the key slots, so whoever has it can try to guess passphrases
offline. argon2id makes each guess slow; a long passphrase is what really
protects it. Keep it as private as the passphrase itself.

**Rollback (stage 3):** nothing to undo.

## Stage 4. Move the data onto the volume

What happens:

1.  A verified backup of the current database.
2.  A **new**, empty Postgres on the volume.
3.  The documented restore into it.
4.  Checks.

The old Docker volume `aether_pgdata` stays untouched as the way back.

**4a. Stop Core (Ctrl+C), disable the backup timer, then take a labelled
backup.**

-   *Disable* the timer, not just stop it: a stopped timer comes back after a
    reboot, and with `Persistent=true` it would back up the new database to
    the old, unencrypted folder.
-   A labelled backup is outside retention: it is never pruned and never
    counted.

```fish
systemctl --user disable --now aether-backup.timer
uv run python -m core.backup --label premove
# expect: one JSON line, "Backup completed", file aether-<time>-premove.dump
set backup (path sort ~/.local/share/aether/backups/aether-*-premove.dump)[-1]
path basename $backup
docker compose exec -T postgres psql -U aether -d aether -Atc 'SELECT count(*), max(id) FROM events'
docker compose exec -T postgres psql -U aether -d aether -Atc 'SELECT count(*) FROM memory_records'
# Note these three numbers: they are what the backup holds (4e checks them).
```

**4b. Install the helper.** It is a fish function with no secrets. It
unlocks, mounts, checks the markers, then starts Postgres; it stops at the
first failure.

```fish
cp scripts/aether-vault.fish ~/.config/fish/functions/
type -q aether-vault; and echo installed           # expect: installed
```

Install the backup drop-in now too, so no backup of the new database can
ever go to the old folder. It sets the folder on the volume, and refuses to
run while the volume is not mounted.

```fish
mkdir -p ~/.config/systemd/user/aether-backup.service.d
cp scripts/systemd/aether-backup.service.d/vault.conf ~/.config/systemd/user/aether-backup.service.d/
systemctl --user daemon-reload
systemctl --user cat aether-backup.service | string match '*vault*'   # expect: the drop-in's lines
```

**4c. Stop the old Postgres, and start a new one on the volume.**
`COMPOSE_FILE` for this fish session only makes every `docker compose` below
use the volume. The old container is replaced; its named volume is kept.

```fish
docker compose stop postgres
set -x COMPOSE_FILE docker-compose.yml:compose.vault.yaml
aether-vault open                # sudo password, then the volume passphrase
# expect: "aether-vault: open; Postgres running on the encrypted volume"
docker inspect aether-postgres-1 --format '{{range .Mounts}}{{.Type}} {{.Source}}{{end}}'
# expect: bind /home/<you>/.local/share/aether/vault/data
docker volume ls -q | string match aether_pgdata   # expect: aether_pgdata (kept)
```

The new cluster is empty. Its owner `aether` gets `POSTGRES_PASSWORD` from
`.env`, as before.

**4d. Restore into it,** following `db-backup-restore.md` section 5 (the
cluster is new):

```fish
docker compose exec -T postgres psql -U aether -d postgres -c 'CREATE ROLE aether_app LOGIN'
docker compose exec -T postgres createdb -U aether aether_restore_check
docker compose exec -T postgres psql -U aether -d postgres -c 'REVOKE ALL ON DATABASE aether_restore_check FROM PUBLIC'
docker compose exec -T postgres psql -U aether -d postgres -c 'GRANT CONNECT ON DATABASE aether_restore_check TO aether_app'
docker compose exec -T postgres pg_restore -U aether -d aether_restore_check --exit-on-error --single-transaction < $backup
# expect: no output
```

**4e. Check before switching anything over:**

```fish
docker compose exec -T postgres psql -U aether -d aether_restore_check -Atc 'SELECT count(*), max(id) FROM events'
# expect: the event numbers noted after the premove backup (4a)
docker compose exec -T postgres psql -U aether -d aether_restore_check -Atc 'SELECT count(*) FROM memory_records'
# expect: the memory count noted in 4a
docker compose exec -T postgres psql -U aether -d aether_restore_check -At < scripts/restore_check.sql
# expect: ok
```

**4f. Put it in place of the new, empty `aether`, then give the app role its
password:**

```fish
docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether RENAME TO aether_empty'
docker compose exec -T postgres psql -U aether -d postgres -c 'ALTER DATABASE aether_restore_check RENAME TO aether'
docker compose exec -T postgres dropdb -U aether aether_empty     # the new, empty one only
uv run python -m core.db_admin set-app-password   # expect: ok: password set; …
uv run python -m core.db                          # expect: ok: connected to aether_app@…
uv run alembic current                            # expect: … (head)
docker compose exec -T postgres psql -U aether -d aether -At < scripts/restore_check.sql   # expect: ok
```

Start Core and check `/ready`: database `ok`.

No memory repair (`db-backup-restore.md` §4 step 3b) is needed here: Core
and the timer were stopped before the `premove` backup, so nothing changed
after it. Step 3b is for restoring an older backup over a database that
kept running.

**Rollback (stage 4), at any point before stage 5.**

-   Nothing was written to the old volume since 4a.
-   If you started Core at the end of 4f, events and memory records
    written since then exist only on the volume. To keep them, back up
    first (with the volume open) and restore that backup into the old
    database afterwards (`db-backup-restore.md` section 4).
    **Not after real memory data exists:** that restore writes it in
    plain text onto the bare disk for good (ADR-026 §7). Roll back only
    while memory holds invented data, or export and delete the real
    records first. The memory repair (`db-backup-restore.md` §4 step 3b)
    doesn't apply here: skip it.
-   Otherwise nothing is lost.

```fish
aether-vault close               # stops the new Postgres, unmounts, locks
set -e COMPOSE_FILE
rm ~/.config/systemd/user/aether-backup.service.d/vault.conf
systemctl --user daemon-reload
docker compose up -d --wait      # the old container definition, on aether_pgdata
uv run python -m core.db         # expect: ok: connected to aether_app@…
systemctl --user enable --now aether-backup.timer
```

## Stage 5. Switch over

**5a. Make compose always use the volume.** Add this line to `.env` (by
hand, in your editor):

```
COMPOSE_FILE=docker-compose.yml:compose.vault.yaml
```

Compose reads it from `.env`, so a plain `docker compose …` in the repo can
no longer start the old database by accident. Core ignores the line. Then
open a **new** terminal (so the session variable from 4c is gone), and check:

```fish
docker compose config --services                  # expect: postgres
docker compose ps --format '{{.Service}} {{.State}}'   # expect: postgres running
```

**5b. Manual backups from this clone go to the volume.** Add to `.env`:

```
AETHER_BACKUP_DIR=~/.local/share/aether/vault/backups
```

```fish
uv run python -m core.backup
ls -l ~/.local/share/aether/vault/backups/         # expect: aether-<time>.dump, -rw-------
```

**5c. The nightly backup.** The drop-in is in place (4b). Update the second
clone (ADR-025 D8), and try both cases:

```fish
git -C ~/Projects/aether-backup pull --ff-only
systemctl --user start aether-backup.service
journalctl --user -u aether-backup.service -n 3 --no-pager   # expect: "Backup completed"
```

Check the guard (Postgres is stopped for a minute):

```fish
aether-vault close
systemctl --user start aether-backup.service       # expect: "Job … failed"
journalctl --user -u aether-backup.service -p err -n 2 --no-pager
# expect: "aether-backup: encrypted volume not mounted (or no AETHER marker); no backup written"
ls -A ~/.local/share/aether/vault                  # expect: nothing
systemctl --user reset-failed aether-backup.service
aether-vault open
systemctl --user enable --now aether-backup.timer
```

**5d. A restore drill from the volume** (`db-backup-restore.md` section 3).
Use the new folder:

```fish
set backup (path sort ~/.local/share/aether/vault/backups/aether-*.dump)[-1]
```

Then follow R2 to R5 there. `restore_check.sql` must print `ok`.

**Rollback (stage 5):**

-   Undo the configuration: remove the `COMPOSE_FILE` and
    `AETHER_BACKUP_DIR` lines from `.env`.
-   Then do stage 4's rollback.
-   **Everything written since stage 4** (new events and memory records)
    exists only on the volume. To keep it, first back it up, then restore
    that backup into the old database (`db-backup-restore.md` section 4).
    **Not after real memory data exists:** that restore writes it in
    plain text onto the bare disk for good (ADR-026 §7). Roll back only
    while memory holds invented data, or export and delete the real
    records first. The memory repair (`db-backup-restore.md` §4 step 3b)
    doesn't apply here: skip it.

## Stage 6. Daily routine

-   **After every boot, before using AETHER:** run `aether-vault open`.
    -   Until then Postgres is not running: Docker doesn't start it at boot
        (`restart: "no"`), and even if something tried, the missing `data/`
        folder makes Docker refuse.
    -   Meanwhile Core still starts. Local chat works, events are skipped,
        and `/ready` says `degraded` with database `unreachable`.
-   **Before shutting down (recommended):** run `aether-vault close`. It
    stops Postgres cleanly, unmounts and locks. If you forget, Postgres
    recovers from its log at the next start.
-   **Check:** `aether-vault status`.
-   **If Postgres crashed:** Docker won't restart it. Run `aether-vault
    open` again (it skips the unlock if the volume is already open).
-   **The nightly backup** fails visibly if the volume is closed at 03:30
    (`systemctl --user --failed`). With `Persistent=true` it does **not**
    rerun by itself; run `systemctl --user start aether-backup.service`
    after `aether-vault open`.

## Stage 7. Clean up the old, unencrypted data (later, only with approval)

Wait until you've run on the volume for a while (at least one week of
nightly backups and one restore drill), and the old data is clearly not
needed.

What each step removes, and what it can't:

-   **The old Docker volume** `aether_pgdata`: the database as it was before
    the move (event metadata and invented memory records only).
    -   After this, the only way back to it is the `premove` backup.
    -   Deleting frees the blocks but doesn't overwrite them. On btrfs and
        an SSD they can't be reliably wiped, and snapshots may still hold
        them.
    -   That is acceptable only because there is no real memory content in it
        (ADR-026 §7).

```fish
docker volume ls -q | string match 'aether_*'      # expect: aether_pgdata
docker ps -a --filter volume=aether_pgdata -q      # expect: nothing (no container uses it)
docker volume rm aether_pgdata
```

Then recreate the same name as a bind to the volume's data folder.

-   Why: a `docker compose` run that misses the override (an explicit
    `-f docker-compose.yml`, another folder, a lost `.env` line) would
    otherwise create a new, empty `aether_pgdata` on the bare disk. Postgres
    would then initialise a fresh database there.
-   With this, even the base file uses the volume's data, and Docker can't
    start Postgres while the volume is locked (the folder doesn't exist).

```fish
docker volume create --driver local --opt type=none --opt o=bind --opt device=$HOME/.local/share/aether/vault/data aether_pgdata
docker volume inspect aether_pgdata --format '{{.Options.device}}'   # expect: …/vault/data
```

Compose may warn that this volume "already exists but was not created by
Docker Compose"; that is expected.

Use exactly this `docker volume rm`, never `docker compose down -v`, which
removes whatever named volumes compose knows about without asking.

-   **The old backups**, in `~/.local/share/aether/backups`, including the
    `premove` one. They are unencrypted, and deleting them is not wiping
    them either (same reason).
-   ADR-025 needs labelled backups deleted within 5 weeks anyway.

```fish
ls -l ~/.local/share/aether/backups/
rm ~/.local/share/aether/backups/aether-*.dump
rm -f ~/.local/share/aether/backups/.lock    # the backup's lock file, if there
ls -A ~/.local/share/aether/backups/                # expect: nothing
sudo chattr +i ~/.local/share/aether/backups        # leave it empty and immutable
```

A backup run that misses the drop-in and the `.env` line (a recopied service
template, say) then fails at the old default folder instead of writing a
plain-text dump there.

-   **Old snapshots.** If stage 0 found snapper or Timeshift, snapshots
    from before the move still hold the old data. Delete them with the
    snapshot tool, or let its retention remove them.
-   **Anonymous volumes.** Postgres run without a mount at some point would
    have left one:

```fish
docker volume ls -q --filter dangling=true         # inspect before removing anything
```

-   **Core dumps (optional).** To stop crash dumps of Postgres or Core,
    which can hold memory text, being kept on disk, create
    `/etc/systemd/coredump.conf.d/nostore.conf` containing:

```
[Coredump]
Storage=none
```

## What is and isn't protected

See ADR-026 §8 for the full list. In short:

-   **Covered:** the database and the backups, while the volume is locked or
    the PC is off: a stolen disk, a discarded disk, a copied backup folder.
-   **Not covered:**
    -   anything while the volume is open (any process with your or root's
        rights can read it);
    -   swap on disk and hibernation;
    -   data written before the move;
    -   memory text you type into shell commands;
    -   memory sent or copied out on purpose: to the model provider a
        call uses, the export file wherever you save it (keep it off
        synced folders, delete it when done), and `/memory` output in
        terminal scrollback or Claude Code transcripts;
    -   the backups' location: they are on the same physical disk as the
        data. An off-machine encrypted copy is a separate issue.
