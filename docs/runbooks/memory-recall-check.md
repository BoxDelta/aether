# Memory recall check on the real PC (Phase 2 exit 1, ADR-024 §8)

The manual half of Phase 2's first exit criterion: a fact saved once is
recalled by **both** real providers, Ollama and Claude. The automated half is
`tests/test_memory_recall.py` (fakes) and `tests/test_memory_recall_live.py`
(live Postgres). **Each step runs only after the owner approves it.**
Commands are fish.

**Who runs what:** the owner runs every step here, because each one needs the
API token from `.env`, and the agent never reads `.env`. The agent may read
the results back from the database (steps 1 and 5) after approval.

**Use only an invented, `public` fact.** The word `TELVARINDO` below is made
up and exists nowhere else. Never use a real fact, name or secret here.

**Claude gets all eligible memory, not just this fact.** Step 4 sends Claude
every approved, unexpired Public and Personal record that retrieval picks
(up to 20, including every keyed `profile` and `preference` record). Step 1
checks that there are none besides this one.

**Permanent rows.** Each `/chat` request adds `events` rows (metadata only,
append-only). The memory record is deleted again in step 6; its
`memory.*` events stay, with IDs and levels only. A daily backup taken while
the record exists keeps it until that backup is pruned (up to about 5 weeks,
ADR-025); harmless, since the fact is invented.

**Run step 6 even if an earlier step fails.** Until it runs, the record is
approved and goes into every later request.

**Cost.** Step 4 makes one short Claude call.

Prerequisites: the encrypted volume open (`aether-vault open` after a
boot, #57), so Postgres is up; migrated (`uv run alembic upgrade head`);
Ollama running; `ANTHROPIC_API_KEY` set; no other Core running on port
8000. Each step below stores the IDs it needs in fish variables
(`$record`, `$rid_ollama`, `$rid_claude`), so nothing has to be copied by
hand. Run all steps in **one** terminal.

------------------------------------------------------------------------

### Step 0: Start Core (localhost only)

```fish
uv run uvicorn core.app:create_app --factory --host 127.0.0.1 --port 8000
```

In a second terminal, load the token into an unexported variable (never
printed; clear it with `set -e token` at the end):

```fish
set -l token (string replace -r '^AETHER_API_TOKEN=' '' (grep '^AETHER_API_TOKEN=' .env))
```

### Step 1: Check there is no other approved memory (read-only)

```fish
echo "SELECT privacy, count(*) FROM memory_records WHERE status = 'approved' GROUP BY privacy;" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

**Expect:** no rows. If there are any, stop, unless you accept that steps
3 and 4 send them (Public and Personal ones to Claude).

### Step 2: Save the fact (owner record, approved at once)

```fish
set -l reply (curl -s http://127.0.0.1:8000/memory \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"content": "The owner calls their test plant TELVARINDO.", "category": "profile", "privacy": "public"}')
echo $reply | jq '{id, status, privacy}'
set -l record (echo $reply | jq -r .id)
```

**Expect:** `status: approved`, `privacy: public` and an `id`, now in
`$record`.

### Step 3: Recall through Ollama

```fish
set -l reply (curl -s http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"override": "ollama", "messages": [{"role": "user", "content": "What do I call my test plant?"}]}')
echo $reply | jq '{request_id, text, provider, model}'
set -l rid_ollama (echo $reply | jq -r .request_id)
```

**Expect:** `provider: ollama` and an answer that names `TELVARINDO`.
Empty output means Core didn't answer: check its terminal.

### Step 4: Recall through Claude

```fish
set -l reply (curl -s http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"allow_cloud": true, "override": "claude", "messages": [{"role": "user", "content": "What do I call my test plant?"}]}')
echo $reply | jq '{request_id, text, provider, model}'
set -l rid_claude (echo $reply | jq -r .request_id)
```

**Expect:** `provider: claude` and an answer that names `TELVARINDO`.

### Step 5: Read back the retrieval events (read-only)

For both requests (read-only transactions, as in the Phase 1 runbook):

```fish
for rid in $rid_ollama $rid_claude
    echo "SELECT provider, memory_count_public, memory_count_personal, memory_count_sensitive, memory_effective_level, memory_record_ids FROM events WHERE event_type = 'memory.context.retrieved' AND request_id = :'rid';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether -v rid=$rid
end
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mTELVARINDO\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

**Expect:** one row per request, naming `ollama` and `claude` in turn,
with **only** the step 2 `id` in `memory_record_ids`, `memory_count_public`
1 and both other counts 0; and a count of **0**: the
fact's text is in no event. `memory_effective_level` is `personal`: the
higher of the request's level (not set, so the default `personal`) and
the memory's (`public`).

### Step 6: Clean up

```fish
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
    http://127.0.0.1:8000/memory/$record -H "authorization: Bearer $token"
set -e token
```

**Expect:** `204`. Stop Core with Ctrl-C.

------------------------------------------------------------------------

## Results

**2026-11-27, owner, on the encrypted volume (#57). Passed.** Models:
Ollama `llama3.1:8b` (id 46e0c10c039e), Claude `claude-sonnet-5-5`.

-   Step 1: no approved memory (0 rows).
-   Step 2: record `56515844-6728-405e-b504-fb8c90666288` saved,
    `approved`, `public`.
-   Step 3 (Ollama): "Your test plant is named TELVARINDO."
    Request `45c87776-9fee-4fdf-9be9-bb2365c7e27f`.
-   Step 4 (Claude): "You call your test plant TELVARINDO."
    Request `592f5c24-be95-43a5-95c5-354c96b6125c`.
-   Step 5: one `memory.context.retrieved` row per request (`ollama`,
    `claude`), each with only the step 2 record, public count 1, personal
    and sensitive 0, effective level `personal`; 0 event rows contain
    the fact's text.
-   Step 6: `204`, record deleted.
-   What differed: the first pass pasted the `REQUEST_ID` and `RECORD_ID`
    placeholders literally (0 rows, then 422) and skipped step 4; it was
    finished with the real IDs. The steps now keep the IDs in variables.
