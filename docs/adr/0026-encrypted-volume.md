# ADR-026: Encrypted volume for the Postgres data and the backups

**Status:** Proposed (owner chose the container file, option 2, on #57)\
**Date:** 2026-11-17\
**Owner:** BoxDelta

## Context

The PC's disk is not encrypted. ADR-024 rejected column encryption (D11)
and gated real memory data on the Postgres data and the backups being on
an encrypted (LUKS) volume (ADR-024 §7, #57). ADR-025 keeps backups on
the same disk, "not encrypted until #57". The owner chose a LUKS
**container file** of 20 GB, not a partition: the root disk is not
converted in place, and no repartitioning is needed.

The host is Arch / GNOME on btrfs, with the fish shell. Postgres runs in
Docker (ADR-020), and backups run from a systemd user timer (ADR-025).
Today the database holds 14 events (max id 44): metadata only, with no
memory content.

The main danger is not the encryption itself. It is a silent fallback:
the volume is locked or not mounted, and Docker or a backup writes to the
bare, unencrypted disk instead. In the worst case Postgres initialises a
fresh, empty database there and AETHER carries on.

This PR is design, docs and helper files only. Everything that changes
the machine is done by the owner from `docs/runbooks/encrypted-volume.md`.

## Decision

### 1. One container file holding data and backups

-   One LUKS2 container file, 20 GB, at
    `~/.local/share/aether/store/aether-vault.luks`, with ext4 inside.
-   Mounted at a fixed point, `~/.local/share/aether/vault`, holding:
    -   `data/`: the Postgres data, bind-mounted to `/var/lib/postgresql`;
    -   `backups/`: `AETHER_BACKUP_DIR` (ADR-025);
    -   two markers, `.aether-vault` and `data/.aether-vault`, written
        once when the volume is made.
-   ext4: simple, journalled, and what Postgres is most often run on; no
    btrfs-in-btrfs.
-   20 GB: the database is tiny today. 1,000-character records and 7 + 4
    compressed dumps leave room for years. ext4 inside a LUKS file can be
    grown later (`truncate`/`fallocate`, `cryptsetup resize`,
    `resize2fs`).
-   **Limit:** backups on the same physical disk as the data protect
    against mistakes (a bad migration, a deleted volume), not against
    losing the disk. An encrypted off-machine copy (ADR-025 D4) is a
    follow-up issue, not this ADR.

### 2. The file on btrfs: allocated, NOCOW, outside snapshots

-   **NOCOW (`chattr +C`) while the file is still empty.** btrfs is
    copy-on-write. A filesystem inside a file gets small random writes
    (Postgres pages, WAL, the ext4 journal). Under CoW, each write moves
    the block, the file fragments into many thousands of extents, and
    performance and free space degrade. `+C` only takes effect on an
    empty file (or on a folder, for new files). It also turns off btrfs
    checksums and compression for that file. Postgres 18 checksums its
    own data pages by default, and ext4 checksums its metadata.
-   **Fully allocated (`fallocate -l 20G`), not sparse.** A sparse file
    takes host space only when written. If the host disk fills up, writes
    inside the volume fail, ext4 goes read-only, and Postgres stops,
    possibly in the middle of writing. Allocating up front makes "the
    host is full" a problem at creation time, not at 03:30. The runbook
    checks free space first (at least 30 GiB free: the file plus a
    margin).
-   **In its own nested btrfs subvolume (`store/`).** A snapshot of the
    home subvolume (snapper, Timeshift) would make the next write to even
    a NOCOW file copy-on-write again. Space would then run out
    unexpectedly, and old encrypted blocks would be kept. Snapshots don't
    include nested subvolumes, so `store/` is left out of them.
    `chattr +C` on `store/` makes every new file in it NOCOW.
-   Allocated blocks read as zeros until written. Someone with the disk
    can tell which parts of the volume have been written, though not what
    was written. Filling the volume with random data first is optional in
    the runbook (one 20 GB write); it is not required here.

### 3. Passphrase only

-   The volume is unlocked only with a passphrase the owner types at
    cryptsetup's prompt. It is never in `.env`, a keyfile on this disk,
    compose, a script, a systemd unit or the shell history. No TPM or
    login auto-unlock in this ADR (see the alternatives).
-   Keep the passphrase in the owner's password manager, one that is
    still available if this PC's disk dies (synced, or with an offline
    copy). An optional second passphrase in a second key slot, written on
    paper and kept somewhere safe, protects against forgetting the first.
-   **A forgotten passphrase means the database AND all its backups are
    lost.** Both are inside the volume. Nobody can recover them.
-   **LUKS header backup** (`cryptsetup luksHeaderBackup`), kept off this
    machine, next to the passphrase:
    -   *Protects against* a damaged header (its first 16 MiB). Without a
        working header the data can't be unlocked even with the right
        passphrase.
    -   *Exposes:* the header holds the key slots. Anyone with the header
        backup, a copy of the container file and a passphrase that was
        valid **when the backup was taken** can unlock it, even after that
        passphrase was changed or removed. After changing a passphrase,
        take a new header backup and destroy the old ones. Without the
        container file it decrypts nothing, but it holds the key slots, so
        whoever has it can guess passphrases offline. argon2id slows each
        guess; a long passphrase is what protects it.
    -   It is written to `/run/user/<uid>` (RAM), copied off the machine,
        then removed. It is never written to this disk.

### 4. Unlock and mount: cryptsetup with sudo

-   Chosen: a plain `sudo cryptsetup open` (it attaches the loop device
    itself and detaches it on `close`), then
    `sudo mount -o nodev,nosuid,noexec` at the fixed mount point. The
    owner runs it through `aether-vault open` (`scripts/aether-vault.fish`,
    section 9).
-   Not chosen: udisks / GNOME Files (`udisksctl loop-setup`, `unlock`,
    `mount`):
    -   it mounts under `/run/media/$USER/<label>`, not at a fixed point
        under the home folder, so the bind mount path would depend on a
        label;
    -   GNOME may offer to remember the passphrase in the keyring, on the
        same disk;
    -   automounting hides when the volume is open.
    -   It would avoid sudo, but sudo here is one prompt and its commands
        are visible in the script.
-   **The mount point is empty and immutable while not mounted.** Before
    the first mount, `~/.local/share/aether/vault` is created empty, mode
    700, then `sudo chattr +i`. Nothing can create a file there, not
    even root (Docker's daemon). Mounting on an immutable folder works,
    and the flag belongs to the folder underneath, not to the mounted
    filesystem.

### 5. Postgres never starts on the bare disk

Fail closed, in layers. Layers 1 to 3 were checked by hand on a throwaway
compose project (see the PR). Layers 1 to 4 hold only when the override is
loaded; layer 6 covers a compose run that misses it.

1.  **No source, no container.** `compose.vault.yaml` bind-mounts
    `~/.local/share/aether/vault/data` with `create_host_path: false`. That
    folder exists only inside the volume. When the volume isn't mounted,
    Docker refuses to start the container ("bind source path does not
    exist") and creates nothing. This also holds when an existing
    container is started again (`docker start`).
2.  **No marker, no Postgres.** The override's entrypoint refuses to run
    the image's entrypoint unless `/var/lib/postgresql/.aether-vault`
    exists. So even a `data/` folder that is not the volume's (made by
    hand, say) never gets a fresh database.
3.  **Not started at boot.** `restart: "no"`: after a reboot the volume
    is locked, and Docker doesn't start Postgres at all. `aether-vault
    open` starts it after unlocking and checking. Trade-off: a crashed
    Postgres is not restarted by Docker; the owner runs `aether-vault
    open` again. (Even with `unless-stopped`, layer 1 would make that boot
    start fail.)
4.  **The mount is the right one.** `aether-vault open` starts Postgres
    only if the mount point is mounted from `/dev/mapper/aether-vault` and
    both markers are present.
5.  **Same target as the default.** The bind mount uses the default
    volume's target, `/var/lib/postgresql`, so it replaces the named
    volume. The image declares `VOLUME /var/lib/postgresql`. If the path
    were left unmounted, Docker would silently create an anonymous volume
    on the unencrypted disk.
6.  **A run without the override still can't use the bare disk.**
    -   Without the override (an explicit `-f docker-compose.yml`, another
        folder or env file, a lost `.env` line), compose falls back to the
        named volume `aether_pgdata`, with `restart: unless-stopped`.
    -   Before stage 7 that is the old, stale database, on the unencrypted
        disk.
    -   So at stage 7 the old volume is not just removed: the name
        `aether_pgdata` is recreated as a local-driver bind to
        `vault/data`. The base file then also uses the volume's data, and
        Docker can't start it while the volume is locked. Without this, a
        missing name would be created empty, and Postgres would initialise
        a fresh cluster on the bare disk.
    -   Until stage 7, `COMPOSE_FILE` in `.env` and the `aether-vault`
        function are what keep compose on the override.

**What Core and `/ready` do when the database is down** (the volume is
locked, or Postgres is not started). This is unchanged:

-   Core still starts. Local chat works, events are skipped with a
    warning, and `/ready` answers `degraded` with database `unreachable`.
    Nothing creates or fills a database.
-   A fresh cluster, if one were ever created anyway, has no `aether_app`
    role. Core would get `auth_failed`; it never writes to an empty
    database as the app role.
-   The memory API (ADR-024 step 6) must answer 503 when the database is
    unreachable, never an empty list or "not found". This is a
    requirement for step 6, recorded in a follow-up issue.

### 6. Backups

-   An opt-in systemd drop-in, `scripts/systemd/aether-backup.service.d/vault.conf`,
    installed at stage 4, before the new database exists. It:
    -   sets `AETHER_BACKUP_DIR=%h/.local/share/aether/vault/backups`;
    -   adds an `ExecStartPre` guard: the vault must be a mount point, with
        its `.aether-vault` marker and its `backups/` folder.
-   Otherwise the unit fails: exit 1, plus one journal line at priority
    err. It writes nothing: the bare mount point is immutable anyway, and
    `core.backup`'s `mkdir` there would fail.
-   The main template is unchanged until the default switches (section 9).
    ADR-025 gets an addition for this.
-   During the move the timer is disabled, not just stopped: after a
    reboot, a stopped timer with `Persistent=true` would run again.
-   The backup goes to the volume only because of the drop-in or the
    `.env` line, not by default. So:
    -   at stage 7 the old default folder is left empty and immutable,
        and a run that misses both fails there instead of writing a
        plain-text dump;
    -   `core.backup` itself refusing a folder outside the volume, once
        the volume is the default, is a follow-up issue.

### 7. Moving the existing data

-   First a verified, labelled backup (`core.backup --label premove`), and
    the event count and max id are written down.
-   Then a **new**, empty Postgres on the volume, and the documented
    restore into it (ADR-025: new cluster, `CREATE ROLE aether_app`, drill
    into `aether_restore_check`, rename, `set-app-password`).
-   Checks: events 14 / max id 44 (or the numbers written down),
    `restore_check.sql` prints `ok`, `core.db` connects as `aether_app`,
    `alembic current` is head. Only then switch compose over.
-   The old Docker volume `aether_pgdata` is **kept**, untouched, as the
    way back. It is deleted (`docker volume rm aether_pgdata`, never
    `docker compose down -v`) only after the owner has run on the volume
    for a while and explicitly approves, with the impact explained in
    the runbook. The name is then recreated as a bind to the volume
    (section 5, layer 6).
-   **Limit: deleting is not wiping.** On btrfs, on an SSD, deleting the
    old volume and the old backups frees their blocks, but doesn't
    overwrite them, and snapshots may still hold them. That is acceptable
    only because the move happens before any real memory data exists:
    today the old volume and backups hold event metadata only. **This is
    why the volume must be in place before the memory API is turned on.**
    Data that was ever written unencrypted can't be reliably removed from
    this disk afterwards.

### 8. Where memory text can still leave the volume

Encryption at rest protects a powered-off PC, a locked volume, a stolen
disk, a disk sent for repair, and a copied backup. While the volume is
open, anything with the owner's or root's rights reads it in plain text.

What is covered:

-   the Postgres data;
-   the backups (from the switch on);
-   psql history: `docker compose exec postgres psql` runs as root with
    `HOME=/root`, in the container's layer under `/var/lib/docker`. The
    override sets `PSQL_HISTORY` to a file on the volume.

What is not covered, and what to do:

-   **Swap and hibernation.** Postgres and Core memory can be paged to
    swap, and hibernation writes all of RAM to disk. CachyOS uses zram (in
    RAM) by default. The runbook checks `swapon --show` for disk swap.
    Don't hibernate, or use encrypted swap. Not changed by AETHER.
-   **Postgres logs** (Docker's container log, under `/var/lib/docker`).
    The override sets `log_error_verbosity=terse` and
    `log_min_error_statement=panic`.
    -   Errors no longer carry DETAIL lines (which quote rows and keys) or
        the failing statement. Checked: with default settings, a failed
        insert put the canary content into the log four times; with these
        settings, none.
    -   Some primary messages can still quote a fragment of input (for
        example `invalid input syntax for type …: "…"`, or a syntax error
        near a token). The memory store sends values as bind parameters
        and checks them first, so these are unlikely, but not impossible.
-   **Core's logs and journald.** Core never logs content (docs/05 §9,
    the store's own rule). Covered by Core's code, not by the volume.
-   **Core dumps** (`/var/lib/systemd/coredump`). A crash dump of Postgres
    or Core can hold memory text. Optional: `Storage=none` in
    `/etc/systemd/coredump.conf.d/`.
-   **`/tmp`.** It is tmpfs (RAM) on Arch; the runbook checks. AETHER
    writes no memory text there: backups are written in their own folder.
-   **Shell history.** The passphrase is typed at cryptsetup's prompt,
    never on the command line. Memory text typed into a `curl` or `psql
    -c` command lands in fish's history: don't, or remove it with
    `history delete`.
-   **Docker image layers.** The image holds no data. Containers and
    volumes are listed in the runbook. Check `docker volume ls` for an
    anonymous volume if Postgres was ever run without a mount.
-   **The old volume and backups.** Section 7: deleted after the move,
    not wiped.
-   **btrfs snapshots** of `/var/lib/docker` or home taken before the
    move. They are kept by the snapshot tool's retention; the runbook
    lists how to check.
-   **The web UI and browser caches.** Future (web UI); not covered here.

### 9. Opt-in now, the default later

-   Now: `compose.vault.yaml` is an override; the default
    `docker-compose.yml` is unchanged. Sentinel and anyone without the
    volume run as before.
-   At the switch, the owner adds `COMPOSE_FILE=docker-compose.yml:compose.vault.yaml`
    to `.env`. Compose reads it, so every plain `docker compose` command
    in the repo uses the volume. Core ignores unknown `.env` keys.
-   This doesn't cover a command that names its own files or env file.
    Until stage 7 recreates `aether_pgdata` as a bind to the volume
    (section 5, layer 6), such a command could start the old database.
-   The fish function `aether-vault` (`open`, `close`, `status`) is the
    daily routine.
-   The backup drop-in is installed at the same stage.
-   **Later, in a small PR after the owner has run the runbook:**
    -   the bind mount becomes the default in `docker-compose.yml`;
    -   the drop-in is folded into the service template;
    -   Sentinel gets its own small override with a named volume (a
        `.github/` change the owner must ask for).
    -   The default should be the bind mount, not "no volume": with no
        mount, the image's `VOLUME` would quietly create an anonymous one
        on the unencrypted disk.

Alternatives considered:

-   **A partition or LVM volume.** Cleaner I/O, but it needs
    repartitioning. The owner chose the file.
-   **Separate volumes for data and backups.** Two passphrases or a
    keyfile; and the same disk either way, so no safety gain.
-   **Converting the root disk.** Risky, and out of scope (#57).
-   **Auto-unlock** (TPM2 with `systemd-cryptenroll`, or a keyfile
    unlocked by the login keyring). Convenient, but the key is then
    released by the same machine an attacker would have.
    Revisit if typing the passphrase becomes a burden.
-   **udisks / GNOME:** section 4.
-   **Encrypting the dump files** (`age`): ADR-025 D7; not needed once
    the backups are in the volume.

## Consequences

-   **Easier:** a stolen or discarded disk, or a copied backup folder,
    reveals no memory. The bare mount point can't receive data, and a
    locked volume stops Postgres and the backup loudly instead of letting
    them run on the bare disk.
-   **Harder:**
    -   after each boot the owner runs `aether-vault open` (sudo password
        plus passphrase) before AETHER has a database;
    -   the nightly backup fails, visibly, if the volume is closed at
        03:30;
    -   Postgres is not restarted automatically after a crash.
-   **Risks:**
    -   a forgotten passphrase loses data and backups together;
    -   the backups are still on the same disk as the data;
    -   data written unencrypted before the move (event metadata only)
        can't be securely wiped;
    -   if the PC shuts down with Postgres running, the volume may be
        detached under it. Postgres recovers from its WAL, but
        `aether-vault close` before shutdown is the clean way.

## Revisit when

-   The owner has run the runbook (make the volume the default; section 9).
-   An off-machine encrypted copy is added (ADR-025 D4).
-   Typing the passphrase becomes a burden (TPM2 unlock).
-   The volume nears 20 GB.
-   AETHER moves off this PC.
