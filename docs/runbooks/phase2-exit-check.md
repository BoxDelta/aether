# Phase 2 exit check (#58, ADR-024 §8)

Checks the three Phase 2 exit criteria (docs/04) and the deliverables on the
owner's PC. Part A collects evidence that already exists (Sentinel's tests
and earlier live runs). Part B is a short live run that adds what those
didn't cover: Sensitive memory on the real providers, and a deletion that
survives a restore. **Each step runs only after the owner approves it.**
Commands are fish, run from the repo root.

**Who runs what:** the owner runs every step in Part B, in **one** terminal
(plus a second one for Core), because the API steps need the token from
`.env` and the agent never reads `.env`. Each step keeps the IDs it needs in
fish variables (`$record`, `$rid_claude`, …), so nothing is copied by hand.
The agent fills in Part A, reads the results back and writes them into the
Results section.

**Use only the invented fact below.** `QUORVETHIL` is made up and exists
nowhere else. Never use a real fact, name or secret here.

**What gets sent where.** Step B3 calls Claude with `allow_cloud`: Claude
gets every approved, unexpired Public and Personal record retrieval picks
(up to 20, every keyed `profile` and `preference` record first). Step B1
shows what that would be. Steps B4 and B5 call local Ollama only.

**Permanent rows and copies.**

-   Each `/chat` request adds `events` rows (metadata only, append-only).
    The record's `memory.*` events stay after it is deleted, with IDs and
    levels only.
-   Step B6's backup holds the record until retention prunes it (about 5
    weeks, ADR-025). That is harmless, because the fact is invented. It is
    on the encrypted volume.
-   Step B8's throwaway database is dropped in step B9.

**Run step B9 even if an earlier step fails.** Until the record is deleted
(B7), it is approved and goes into every later local request with
`include_sensitive`.

**Cost.** Step B3 makes one short Claude call.

------------------------------------------------------------------------

## Part A: evidence that already exists

The agent fills this in from `main` and Sentinel. Nothing here touches the
database or a provider.

-   **Exit 1, cross-provider recall:**
    -   tests `tests/test_memory_recall.py` (fakes with the real adapters'
        limits) and `tests/test_memory_recall_live.py` (live Postgres, in
        Sentinel);
    -   the live check on both real providers,
        `docs/runbooks/memory-recall-check.md` (passed 2026-11-27).
-   **Exit 2, no context above a provider's level:** tests
    `test_retrieval.py`, `test_memory_chat.py`, `test_memory_recall.py`
    (Sensitive stays local after failover; the cloud without `allow_cloud`
    gets no memory), `test_untrusted_ollama.py`, `test_routing.py`,
    `test_claude.py`. Part B steps B3–B5 add the real providers.
-   **Exit 3, deletion and restore:** `tests/test_memory_restore_live.py`
    (Sentinel: backup, delete, reject, raise a level, correct, create,
    restore, repair, check); the restore drill from the encrypted volume
    (#57, stage 5). Part B steps B6–B9 add a deletion on the real database.
-   **Deliverables (docs/04):**
    -   schema: migrations 0004–0006;
    -   a privacy level on every record: the CHECK on `memory_records`;
    -   owner-approved create and update: the approval flow (steps 8–9) and
        model proposals (ADR-033);
    -   retrieval: structured and full-text search (step 11), no pgvector;
    -   inspect, correct, export and delete: `/memory` (steps 6, 9, 10);
    -   backup and tested restore: ADR-025, ADR-026, step 16.

------------------------------------------------------------------------

## Part B: live run

Prerequisites:

-   the encrypted volume is open (`aether-vault open`);
-   the database is migrated to head;
-   Ollama is running on its trusted local host;
-   `ANTHROPIC_API_KEY` is set;
-   no other Core is running on port 8000;
-   the backup timer won't fire during the run. It runs between 03:30 and
    03:40, so don't run this then.

### Step B0: Prerequisites (read-only)

```fish
mountpoint ~/.local/share/aether/vault        # is a mountpoint
docker compose ps postgres                    # STATUS (healthy)
uv run alembic current                        # … (head)
uv run python -m core.db                      # ok: connected to aether_app@…
ollama list                                   # llama3.1:8b listed
grep -c '^AETHER_BACKUP_DIR=.*vault/backups' .env   # 1: backups go to the volume (prints a count only)
```

If the last line prints 0, set `AETHER_BACKUP_DIR` to
`~/.local/share/aether/vault/backups` in `.env` first
(`docs/runbooks/encrypted-volume.md` stage 5). Without it, step B6 fails on
the old, immutable folder and writes nothing.

Start Core in a second terminal (localhost only), then load the token in
the first. The token is never printed. Don't run `echo $token`, or `set`
with no arguments, and don't use `curl -v`.

```fish
uv run uvicorn core.app:create_app --factory --host 127.0.0.1 --port 8000
```

```fish
set -l token (string replace -r '^AETHER_API_TOKEN=' '' (grep '^AETHER_API_TOKEN=' .env))
```

### Step B1: What memory exists (read-only)

```fish
echo "SELECT status, privacy, count(*) FROM memory_records GROUP BY 1, 2 ORDER BY 1, 2;" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mQUORVETHIL\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

**Expect:**

-   Counts only; no content is shown. Any approved Public or Personal
    record goes to Claude in step B3. If you don't want that, stop here.
-   A marker count of 0.

### Step B2: Save a Sensitive fact (owner record)

```fish
set -l reply (curl -s http://127.0.0.1:8000/memory \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"content": "The owner calls their test medicine QUORVETHIL.", "category": "profile", "privacy": "sensitive"}')
echo $reply | jq '{id, status, privacy}'
set -l record (echo $reply | jq -r .id)
```

**Expect:** `status: approved`, `privacy: sensitive` and an `id`, now in
`$record`.

### Step B3: Claude never gets it, even when asked to include Sensitive memory

```fish
set -l reply (curl -s http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"allow_cloud": true, "include_sensitive": true, "override": "claude", "messages": [{"role": "user", "content": "What do I call my test medicine?"}]}')
echo $reply | jq '{request_id, text, provider}'
set -l rid_claude (echo $reply | jq -r .request_id)
```

**Expect:** `provider: claude`, and an answer that does **not** name
`QUORVETHIL`: it says it doesn't know.

### Step B4: Local Ollama without the per-request approval

```fish
set -l reply (curl -s http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"override": "ollama", "messages": [{"role": "user", "content": "What do I call my test medicine?"}]}')
echo $reply | jq '{request_id, text, provider}'
set -l rid_local_no (echo $reply | jq -r .request_id)
```

**Expect:** `provider: ollama`, and no `QUORVETHIL` in the answer.

### Step B5: Local Ollama with `include_sensitive`

```fish
set -l reply (curl -s http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"override": "ollama", "include_sensitive": true, "messages": [{"role": "user", "content": "What do I call my test medicine?"}]}')
echo $reply | jq '{request_id, text, provider}'
set -l rid_local_yes (echo $reply | jq -r .request_id)
```

**Expect:** `provider: ollama`, and an answer that names `QUORVETHIL`.

Then read back the retrieval events (read-only):

```fish
for rid in $rid_claude $rid_local_no $rid_local_yes
    echo "SELECT provider, memory_count_public, memory_count_personal, memory_count_sensitive, memory_effective_level, memory_record_ids FROM events WHERE event_type = 'memory.context.retrieved' AND request_id = :'rid';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether -v rid=$rid
end
```

**Expect:** one row per request.

-   `claude`: `memory_count_sensitive` **0**, and `$record` is not in
    `memory_record_ids`.
-   `ollama` (B4): sensitive **0**, and `$record` is not in the IDs.
-   `ollama` (B5): sensitive **1**, `$record` is in the IDs, and the
    effective level is `sensitive`.
-   The public and personal counts match the approved records B1 showed
    (Claude and B4 may get them too).

### Step B6: Back up while the record exists

```fish
uv run python -m core.backup
set -l backup (path sort ~/.local/share/aether/vault/backups/aether-*.dump)[-1]
path basename $backup
```

**Expect:** one `Backup completed` JSON line, and the name of the new file
(today's time).

### Step B7: Delete the record

```fish
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
    http://127.0.0.1:8000/memory/$record -H "authorization: Bearer $token"
set -e token
```

**Expect:** `204`. Then stop Core with Ctrl-C: the repair runs with Core
stopped.

### Step B8: Restore into a throwaway database and repair it

This follows `docs/runbooks/db-backup-restore.md` section 3 (R2–R4) and
step 3b. It never touches the live `aether` database. It creates
`aether_restore_check`, which B9 drops.

```fish
docker compose exec -T postgres createdb -U aether aether_restore_check
docker compose exec -T postgres psql -U aether -d postgres -c 'REVOKE ALL ON DATABASE aether_restore_check FROM PUBLIC'
docker compose exec -T postgres psql -U aether -d postgres -c 'GRANT CONNECT ON DATABASE aether_restore_check TO aether_app'
docker compose exec -T postgres pg_restore -U aether -d aether_restore_check --exit-on-error --single-transaction < $backup
echo "SELECT count(*) AS restored FROM memory_records WHERE id = :'rec';" | docker compose exec -T postgres psql -U aether -d aether_restore_check -v rec=$record
uv run python -m memory.restore --into aether_restore_check --dry-run
uv run python -m memory.restore --into aether_restore_check
echo "SELECT count(*) AS after_repair FROM memory_records WHERE id = :'rec';" | docker compose exec -T postgres psql -U aether -d aether_restore_check -v rec=$record
docker compose exec -T postgres psql -U aether -d aether_restore_check -At < scripts/restore_check.sql
```

**Expect:**

-   `restored` is **1**: the backup brought the deleted record back.
-   Each `memory.restore` run ends with one `Memory repair completed` line
    (the dry run's has `dry_run` true and is rolled back), with
    `deleted_again` 1 and `set_pending` 0.
-   `after_repair` is **0**: the deletion survives the restore.
-   `restore_check.sql` prints `ok`.

### Step B9: Clean up (always)

This removes only `aether_restore_check`. Check the name before you press
Enter. If B7 didn't run, delete the record first, with the step B7
command, Core running and the token loaded. If `$record` is empty, B2
created nothing and there is nothing to delete.

```fish
docker compose exec -T postgres dropdb -U aether aether_restore_check
echo "SELECT count(*) AS live FROM memory_records WHERE id = :'rec';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether -v rec=$record
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mQUORVETHIL\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
set -e token    # again, in case a step failed before B7
```

**Expect:** `live` 0; `rows_with_marker` 0: the fact's text is in no
event.

------------------------------------------------------------------------

## Results

**Result: passed, 2026-11-27.** All three exit criteria hold on the
owner's PC.

**Part A, re-checked 2026-11-27 by the agent on `main` (67ea2ab).**
Sentinel passed on that commit, including the live Postgres job. Every
test file listed in Part A is on `main`. Locally, with the database at
migration 0008 (head): `uv run pytest -q` gave 1837 passed, 251 skipped
(the live tests); `env AETHER_DB_TESTS=1 uv run pytest -q -m db` gave 251
passed. (First checked on 85b39ef: 1750 passed, 241 skipped.)

**Part B, run 2026-11-27 by the owner** (about 20:26–20:31 UTC), output
read back by the agent:

-   **B0:** the volume is a mountpoint; Postgres healthy; `alembic
    current` 0008 (head); Core connects as `aether_app`; `llama3.1:8b`
    listed; `AETHER_BACKUP_DIR` points at the volume (count 1).
-   **B1:** 0 memory records (so Claude got no other memory in B3); 0
    events with the marker.
-   **B2:** record `f36ee8eb-e4e0-4d9b-9298-696f229754a8` saved,
    `approved`, `sensitive`.
-   **B3 (Claude, `allow_cloud` and `include_sensitive`):** `provider:
    claude`; the answer doesn't name the fact ("I don't have anything
    saved about this").
-   **B4 (Ollama, no `include_sensitive`):** `provider: ollama`; "You
    haven't shared the name of your test medicine yet."
-   **B5 (Ollama, `include_sensitive`):** `provider: ollama`; "You've
    called it QUORVETHIL."
-   **Retrieval events:** Claude and B4: `memory_count_sensitive` 0, no
    record IDs; B5: `memory_count_sensitive` 1, IDs `{f36ee8eb…}`,
    effective level `sensitive`. (The empty rows show `personal`, the
    request's default level.)
-   **B6:** `Backup completed`, `aether-20261127T202955Z.dump` on the
    volume (`kept` 1, `pruned` 1: the day's earlier backup, by D5
    retention).
-   **B7:** `DELETE` returned 204; Core stopped.
-   **B8:** `restored` 1 (the backup brought the record back); dry run
    and real run each `Memory repair completed` with `events_copied` 1,
    `deleted_again` 1, `set_pending` 0, `other_events_lost` 0;
    `after_repair` 0 (the deletion survives the restore);
    `restore_check.sql` printed `ok`.
-   **B9:** `aether_restore_check` dropped; `live` 0;
    `rows_with_marker` 0 (the fact's text is in no event).

**Exit criteria:**

1.  Cross-provider recall: `docs/runbooks/memory-recall-check.md`
    (passed 2026-11-27) and the recall tests (Part A).
2.  No context above a provider's level: B3–B5 on the real providers,
    plus the tests in Part A.
3.  Deletion and restore: B6–B9 on the real database, plus
    `tests/test_memory_restore_live.py`.

Left behind on purpose: the B6 backup holds the invented record until
retention prunes it (about 5 weeks); the record's `memory.*` events stay
(IDs and levels only).
