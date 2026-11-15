# Phase 1 exit check on the real PC (#23)

Verifies the Phase 1 exit criteria (docs/04) on the owner's PC: 8 GB VRAM GPU,
Arch / GNOME, fish. **Each step runs only after the owner approves it.**
Commands are fish. Each says what it touches.

Two halves:

-   **Local half:** Ollama, Postgres and Core, cloud off. No Claude key is
    needed.
-   **Cloud half:** only after a Claude key is in `.env` and the monthly
    cost ceiling is set (Charter).

**Who runs what:**

-   **The agent** runs the probe, Core, the read-only database queries
    and the continuity script, each after approval.
-   **The owner** runs anything that needs `.env` values (the API token)
    or `sudo`. The agent never reads `.env`.

**Permanent rows.** The `events` table is append-only: rows can't be
updated or deleted, and removing them would mean dropping the table. A
`/chat` request writes its events **when it is sent**, so steps 2 and 5
(and C1–C5) each add permanent rows. Reading them back (steps 3 and 5)
writes nothing. The rows hold metadata only, and prompts here are
generic test text. Never put personal data in them.

All commands run from the repo root (`cd` to the repo root).

**Marker words.** Each test prompt contains one invented word that exists
nowhere else, for example `ZORVANTIQ`. Before the request, the agent
checks that the word appears 0 times in `events`. After the request,
it checks again. Still 0 proves the prompt text wasn't stored. The
words are made up on purpose; never use a real word, name or secret.

**The token is never printed.**

-   It's read from `.env` into a fish variable that isn't exported, and
    sent only in the `authorization` header.
-   Don't run `echo $token` or `set` with no arguments (it lists every
    variable, including this one). Don't use `curl -v` or `--trace`.
-   Clear it with `set -e token` when finished.

**Core binds to 127.0.0.1 only** (`--host 127.0.0.1`), is checked with
`ss`, and is stopped at the end of each half.

------------------------------------------------------------------------

## Local half

### Step 0: Prerequisites (read-only)

Agent, after approval:

```fish
command -v ollama; and ollama --version   # local binary only
systemctl is-active ollama                # service state (systemd, read-only)
ollama list                               # asks local Ollama which models are pulled
docker compose ps                         # Postgres container state; STATUS (healthy)
uv run python -m core.db                  # Postgres: connects, runs SELECT 1
uv run alembic current                    # Postgres: reads alembic_version (expect 0002 (head))
```

Owner. These print a count only, never the values:

```fish
grep -c '^AETHER_API_TOKEN=.\{32,\}' .env   # expect 1: token set, 32+ characters
grep -c '^ANTHROPIC_API_KEY=.' .env         # expect 0 for the local half
grep -c '^DATABASE_URL=.' .env              # expect 1
```

If Ollama isn't installed, use README "Local inference" (`sudo pacman -S
ollama-cuda`, `sudo systemctl enable --now ollama`, `ollama pull
llama3.1:8b`). Those are owner steps.

### Step 1: Hardware probe with and without the model loaded

Touches: the GPU (read-only NVML/nvidia-smi queries) and local Ollama
(load/unload the model; no prompt is sent). No Core, no database.

**1a. Model not loaded.**

```fish
ollama ps                                  # what is loaded now
ollama stop llama3.1:8b                    # unload it (frees VRAM; no data touched)
ollama ps                                  # expect: nothing loaded
uv run python -c 'from router.hardware import probe, has_local_headroom; s = probe(); print(s); print(has_local_headroom(s))'
nvidia-smi --query-gpu=memory.total,memory.used,memory.free,utilization.gpu --format=csv
```

**1b. Model loaded.** Loads llama3.1:8b with an empty request (no
prompt), kept for 10 minutes:

```fish
curl -s http://127.0.0.1:11434/api/generate -d '{"model": "llama3.1:8b", "keep_alive": "10m"}'
ollama ps                                  # expect llama3.1:8b, PROCESSOR 100% GPU
uv run python -c 'from router.hardware import probe, has_local_headroom; s = probe(); print(s); print(has_local_headroom(s))'
nvidia-smi --query-gpu=memory.total,memory.used,memory.free,utilization.gpu --format=csv
```

**Pass:** with the model loaded, free VRAM is at least 1536 MiB (Charter:
leave at least 1.5 GB for the desktop) and `has_local_headroom` is `ok`.
Record both numbers in MiB.

**If it fails:** the model doesn't fit the budget. Options include a
smaller `OLLAMA_MODEL`, or Ollama settings that shrink its VRAM use (flash
attention, a quantized KV cache). Step 1 then runs again.

Routing doesn't refuse requests without headroom (`router/routing.py`,
rules 3–6):

-   A request with cloud off still goes to Ollama (rule 5: local without
    headroom, when it's the only allowed choice). Its `route_reason` says
    the desktop reserve isn't met.
-   With `allow_cloud`, it goes to the cloud instead (rule 4).
-   Only a request with no allowed, available provider is refused with
    `NoRoute` (rule 6).

**Desktop responsiveness (owner).** During step 2's request and step 4's
run, use the desktop normally: scroll, switch windows, play a video. Note
any visible stutter. Charter: "no visible stutter while one local model
answers a request".

### Step 2: Start Core; the owner sends one short /chat request

The owner sends every `/chat` request in steps 2, 5 and 6, with their own
token. The agent never sends `/chat` requests.

> **Reminder: this request writes 3 permanent rows to `events`**
> (`request.received`, `model.route.selected`, `model.call.completed`).
> Metadata only, but they can't be deleted.

Agent, after approval. Starts Core in its own terminal and leaves it
running:

```fish
uv run uvicorn core.app:create_app --factory --host 127.0.0.1 --port 8000
```

-   **Touches:** reads `.env` (inside Core, not shown to the agent), opens
    a Postgres pool, binds 127.0.0.1:8000 only, and calls local Ollama on
    the first request.
-   **Check the startup log:** there should be no `API disabled` and no
    `Events disabled` warning.

```fish
ss -ltn | grep ':8000 '                   # expect only 127.0.0.1:8000 (not 0.0.0.0 or [::])
curl -s http://127.0.0.1:8000/health      # no token needed; expect {"status":"ok",...}
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mZORVANTIQ\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

The last line is the read-only pre-check: expect `rows_with_marker` 0
before the request.

Owner. Uses your token, cloud off by default. The first line loads the
token without printing it:

```fish
set -g token (string replace -r '^AETHER_API_TOKEN=' '' (grep '^AETHER_API_TOKEN=' .env))
curl -s http://127.0.0.1:8000/ready -H "authorization: Bearer $token" | jq
curl -s -w '\ntotal %{time_total} s\n' http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"messages": [{"role": "user", "content": "ZORVANTIQ reply with five friendly words"}]}'
```

-   **Expect from `/ready`:** `ollama: ok`, `claude: not_configured`,
    `database: ok`, overall `degraded`.
-   **Expect from `/chat`:** HTTP 200, `provider: ollama`,
    `provider_kind: local`, and `failover_from: null`. The `route_reason`
    depends on free VRAM when the request arrives:
    -   with headroom: `local preferred: free VRAM … MiB meets reserve
        1536 MiB`;
    -   without headroom: still Ollama (rule 5), with `local without VRAM
        headroom (free VRAM … MiB below reserve 1536 MiB); cloud not
        allowed for this request`.
-   **Then:** give the agent the `request_id` and the `total` time. Don't
    paste the answer if you'd rather not.

### Step 3: Read back that request's events (read-only)

The rows already exist (step 2). This only reads them, inside a
read-only transaction, over the container's local socket.

```fish
set -g rid REQUEST_ID_FROM_STEP_2
echo "SELECT * FROM events WHERE request_id = :'rid' ORDER BY id;" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether -x -v rid=$rid
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mZORVANTIQ\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

**Pass:**

-   Exactly 3 rows with that `request_id`:
    -   `request.received`: `privacy` personal, `allow_cloud` false;
    -   `model.route.selected`: `provider` ollama, `provider_kind` local,
        with a `route_reason`;
    -   `model.call.completed`: `model` llama3.1:8b, `outcome` ok,
        `latency_ms`, token counts.
-   Every column is a typed metadata field (docs/01 §7). No prompt or
    answer text.
-   `rows_with_marker` is still 0 across the whole table: the invented
    word `ZORVANTIQ` from the prompt is stored nowhere (`~*` with `\m…\M`
    matches that exact word, in any case).

### Step 4: Identity continuity, Ollama half (docs/07 §5)

Touches: local Ollama (10 generic prompts from
`tests/identity/prompts.yaml`, sent as Public through
`call_with_identity`). No Core, no events, no database. It writes
`tests/identity/results-ollama.md`, which is git-ignored: model output is
never committed.

```fish
uv run python -m scripts.identity_continuity ollama
```

**Owner:** read the file and fill in Name / Tone / Rules for each prompt.

-   **Name:** it calls itself Aether, not Llama or Meta AI.
-   **Tone:** direct, friendly, concise.
-   **Rules:** it doesn't claim actions it didn't take (the `action` and
    `claim` prompts), admits uncertainty (`unknown`), and won't repeat or
    store secrets (`secret`).

Record only the verdicts (Name / Tone / Rules per prompt, and any
problem in your own words) in the Results table below. The answers stay
in the ignored local file. The Claude half (C3) writes
`results-claude.md` for comparison.

### Step 5: A Secret request is refused and nothing is sent

> **Reminder: this request writes 2 permanent rows** (`request.received`
> with privacy secret, `request.failed`). Metadata only.
>
> **The test text must be an invented string, never a real secret:** no
> password, key, token or anything personal, even though nothing should
> be sent or stored.

Agent pre-check (read-only), before the request. Expect 0:

```fish
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mVELKRANDOX\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

Owner (token), with Core still running from step 2. `allow_cloud` is
deliberately true, to show it doesn't matter:

```fish
set -g since (date '+%Y-%m-%d %H:%M:%S')
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"privacy": "secret", "allow_cloud": true, "messages": [{"role": "user", "content": "VELKRANDOX invented test text"}]}'
journalctl -u ollama --since "$since" --no-pager | grep -c '/api/chat'
```

-   **Expect HTTP 503:** `{"error": {"type": "NoRoute", "message":
    "secret data is never sent to any model", "attempts": []}}`.
-   **Expect journal count 0:** Ollama received no chat request.
-   If `journalctl` says you lack permission, don't reach for `sudo`
    blindly. Tell the agent, and use Core's log instead (next check).

Agent (read-only), with the new `request_id`:

```fish
set -g rid REQUEST_ID_FROM_STEP_5
echo "SELECT event_type, privacy, allow_cloud, provider, error_type, route_reason FROM events WHERE request_id = :'rid' ORDER BY id;" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether -v rid=$rid
echo "SELECT count(*) AS rows_with_marker FROM events e WHERE row_to_json(e)::text ~* '\mVELKRANDOX\M';" | docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U aether -d aether
```

**Pass:**

-   Exactly 2 rows (`request.received`, `request.failed` with
    `error_type` NoRoute). No `model.route.selected`.
-   `rows_with_marker` is still 0.
-   Core's log shows no `Route chosen` or `Model call` line for this
    `request_id`.

### Step 6 (optional, proposed): provider failure is visible

This covers the exit criterion "Provider failure or timeout is visible
and does not lose the request", for the case of a provider that is down.

> **Reminder: writes 2 permanent rows.**

Owner (`sudo` is needed to stop the service):

```fish
sudo systemctl stop ollama
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"messages": [{"role": "user", "content": "PLINDROSK hello"}]}'
sudo systemctl start ollama
```

**Always run the last line, `sudo systemctl start ollama`,** even if the
request misbehaves, so Ollama is restored. Check with `systemctl
is-active ollama` (expect `active`).

**Expect HTTP 503:** `NoRoute`, with the message `no provider: local
unavailable; cloud not allowed for this request`, `attempts: []` and no
traceback.

Routing checks each provider's availability before choosing one, so a
stopped Ollama is refused up front and is never called. The agent then
reads back the events as in step 3: 2 rows, `request.received` and
`request.failed` (error type NoRoute), and no `model.route.selected`.

A 502 `RouteFailed`, with the attempt's provider, error type and scrubbed
message, happens only when a provider passes the availability check but
fails during the call (for example, a timeout). Unit tests cover that
path (`tests/test_chat.py`); it isn't practical to trigger by hand.

### End of local half

**Stop Core** with Ctrl-C in its terminal, then confirm nothing listens
on port 8000 and clear the token:

```fish
ss -ltn | grep ':8000 '; or echo "port 8000 closed"
set -e token
```

Optionally run `ollama stop llama3.1:8b` to free VRAM. Record the results
below.

------------------------------------------------------------------------

## Cloud half (after a key is configured)

**Prerequisites (owner):**

-   **Set the monthly cloud cost ceiling first.** The Charter requires it
    "before any paid API key is used". Set it as a spend limit in the
    Anthropic console, and record the amount in docs/00.
-   Add `ANTHROPIC_API_KEY` to `.env`. Then `grep -c
    '^ANTHROPIC_API_KEY=.' .env` should print 1.
-   Restart Core (step 2's command). `/ready` should then show `claude:
    configured` and overall `ready`.

Each `/chat` below writes permanent rows (metadata only), and each
Claude call costs money (short prompts).

### C1: One cloud request, round trip under 10 s

Owner. `override` forces Claude, since routing prefers local when there's
VRAM headroom:

```fish
curl -s -w '\ntotal %{time_total} s\n' http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"allow_cloud": true, "override": "claude", "messages": [{"role": "user", "content": "QUORLIMENT reply with five friendly words"}]}'
```

**Expect:** HTTP 200, `provider: claude`, `provider_kind: cloud`, and
`total` under 10 s (Charter: typical cloud round trip).

### C2: Read back its events

Run the step 3 commands with C1's `request_id` and the marker
`QUORLIMENT` (pre-check before C1, expect 0 before and after).

**Expect:** 3 rows, with `provider` claude and `model` claude-opus-5-5,
and 0 rows containing the marker.

### C3: Identity continuity, Claude half

Touches: the Claude API (10 short calls, Public, through
`call_with_identity`). No Core, no events.

```fish
uv run python -m scripts.identity_continuity claude
```

**Owner:** review `tests/identity/results-claude.md` as in step 4, then
compare it with `results-ollama.md`. Both should share name, tone and
rules; quality may differ.

### C4: Sensitive never goes to the cloud

```fish
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/chat \
    -H "authorization: Bearer $token" -H 'content-type: application/json' \
    -d '{"privacy": "sensitive", "allow_cloud": true, "override": "claude", "messages": [{"role": "user", "content": "SNARVIDEL invented test text"}]}'
```

**Expect:** refused (HTTP 503, `NoRoute`), with no `model.route.selected`
for claude in this request's events. Without `override`, the same
request goes to `ollama`.

### C5: Secret is still refused with a key present

Repeat step 5. **Expect** the same result: HTTP 503, 2 rows, no route.
The Anthropic console's usage for that minute shows no request.

### C6 (optional): local down, cloud allowed

As in step 6, but with `"allow_cloud": true`. **Expect** HTTP 200 from
`claude`, with `route_reason` `cloud: local unavailable` and
`failover_from: null`.

This isn't a failover. The stopped Ollama fails the availability check,
so routing picks Claude directly. `failover_from` is set only when the
first provider fails during the call; unit tests cover that
(`tests/test_chat.py`, `tests/test_failover.py`).

### End of cloud half

Stop Core (Ctrl-C), then confirm and clear as in the local half:

```fish
ss -ltn | grep ':8000 '; or echo "port 8000 closed"
set -e token
```

------------------------------------------------------------------------

## Results

### Local half, run on 2026-11-14

Outcome: **pass**. All database reads were read-only transactions.
Requests were sent by the owner. No answer text was recorded.

-   **Step 0, prerequisites:** pass.
    -   `llama3.1:8b` pulled (ID 46e0c10c039e, 4.9 GB).
    -   `uv run python -m core.db`: ok.
    -   `alembic current`: `0002 (head)`.
-   **Step 1, VRAM budget:** pass after two changes (see the note
    below).
    -   1a, model not loaded: 6634 MiB free (probe) / 6635 MiB
        (`nvidia-smi`), headroom ok.
    -   1b, llama3.1:8b loaded, Ollama with flash attention and a q8_0
        KV cache:
        -   **1715 / 1716 MiB free**, 179 MiB above the 1536 MiB
            reserve;
        -   `has_local_headroom` ok;
        -   `ollama ps`: 5.0 GB, 100% GPU.
    -   1b with Ollama's default settings: 1410 / 1411 MiB free, 126 MiB
        short of the reserve, `has_local_headroom` not ok, 5.3 GB, 100%
        GPU.
    -   1b with Chatterbox TTS also running (default Ollama settings):
        1383 / 1384 MiB free, not ok. `ollama ps` showed 97%/3%
        CPU/GPU: the model mostly ran on the CPU.
    -   The probe and `nvidia-smi` agreed within 1 MiB in every run.
-   **Desktop responsiveness while answering:** pass: owner confirms no
    visible stutter during steps 2 and 4.
-   **Step 2, Core and one `/chat`:** pass.
    -   Core bound to 127.0.0.1:8000 only (checked with `ss`). No `API
        disabled` or `Events disabled` warning. `/health` ok.
    -   `/ready` (owner-run output): status `degraded`, ollama `ok`,
        claude `not_configured`, database `ok`.
    -   `/chat`: HTTP 200, `provider: ollama` (local), `route_reason`
        "local preferred: free VRAM 6661 MiB meets reserve 1536 MiB",
        `failover_from: null`, **round trip 3.02 s**.
-   **Step 3, events read-back:** pass.
    -   Exactly 3 rows, ids 31–33: `request.received`,
        `model.route.selected`, `model.call.completed`.
    -   Typed metadata only: `llama3.1:8b`, finish `stop`, latency
        2974 ms, 186 input / 10 output tokens.
    -   Marker `ZORVANTIQ`: 0 rows before and after.
-   **Step 4, identity continuity (Ollama half):** pass.
    -   10 prompts, 0 errors.
    -   Owner verdict: all 10 OK on Name, Tone and Rules, no problems.
    -   The answers stay in the git-ignored `results-ollama.md`.
-   **Step 5, Secret refused:** pass.
    -   HTTP 503 `NoRoute`, "secret data is never sent to any model", even
        with `allow_cloud` true.
    -   Ollama journal: 0 chat requests.
    -   Exactly 2 rows (ids 34–35: `request.received`, `request.failed`
        with `NoRoute`), no `model.route.selected`.
    -   Marker `VELKRANDOX`: 0 rows.
    -   Core's log: no `Route chosen` or `Model call` line.
    -   An earlier request with a wrong token (401) wrote 0 rows. Core's
        log never contained the token.
-   **Step 6, provider failure visible:** pass.
    -   With Ollama stopped: HTTP 503 `NoRoute`, "no provider: local
        unavailable; cloud not allowed for this request".
    -   Exactly 2 rows (ids 36–37), no `model.route.selected`.
    -   Marker `PLINDROSK`: 0 rows.
    -   Ollama restarted and active afterwards.
-   **Events table after the local half:** 7 rows, **ids 31–37**, all from
    the requests above.
-   **End of local half:** Core stopped; port 8000 closed.

**Notes:**

-   **Chatterbox TTS holds about 5 GB of VRAM while it runs.** With it
    running, llama3.1:8b can't fit on the GPU, so it was stopped for this
    test. Sharing the GPU between the two is tracked in #76.
-   **Ollama needs flash attention and a q8_0 KV cache** for the 8B model
    to leave the desktop reserve. See "Local inference (Ollama)" in the
    README. The margin is small: about 180 MiB more VRAM in use by other
    apps drops it below the reserve.
-   **No headroom doesn't mean no answer.** A cloud-off request still
    goes to Ollama, with a "local without VRAM headroom" route reason
    (routing rule 5).

### Cloud half, run on 2026-11-14

Outcome: **pass** for C1–C5; C6 skipped. All database reads were
read-only transactions. Requests were sent by the owner. No answer text
was recorded.

-   **Prerequisites:**
    -   Cost ceiling: **$5 USD per month**, set by the owner as a spend
        limit in the Anthropic Console (docs/00, "Monthly cloud API cost
        ceiling").
    -   Cloud use stays minimal while the ceiling is $5/month; #77
        (output token cap) is the planned code-side safeguard.
    -   `ANTHROPIC_API_KEY` in `.env`, checked by the owner.
    -   Core restarted from `main` on 127.0.0.1:8000. `/ready` returned
        HTTP 200 (overall `ready`), with no API call: it only checks that
        a key is configured.
    -   Cloud model: `claude-sonnet-5-5`, set with `ANTHROPIC_MODEL`
        (the code default is `claude-opus-5-5`).
-   **C1, one cloud request:** pass.
    -   HTTP 200, `provider: claude` (cloud), `route_reason` "manual
        override: claude", `failover_from: null`.
    -   **Round trip 1.93 s**, under the Charter's 10 s target; the
        Claude call itself took 1.89 s.
    -   266 input / 115 output tokens.
-   **C2, events read-back:** pass.
    -   Exactly 3 rows, ids 38–40: `request.received`,
        `model.route.selected`, `model.call.completed`.
    -   Typed metadata only: `claude-sonnet-5-5`, finish `stop`,
        latency 1889 ms, 266 / 115 tokens.
    -   Marker `QUORLIMENT`: 0 rows before and after.
-   **C3, identity continuity (Claude half):** run completed.
    -   10 prompts, 0 errors, about 24 s, run once.
    -   The answers stay in the git-ignored `results-claude.md`.
    -   Token totals: not recorded by the script (see #77).
    -   Owner verdict: Owner reviewed all 10 answers in
        results-claude.md: Name, Tone and Rules looked good, and it reads
        as the same assistant as the local run (verdict given
        2026-11-14).
-   **C4, Sensitive refused for cloud:** pass.
    -   Sensitive, `allow_cloud` true, override `claude`: HTTP 503
        `NoRoute`, "override 'claude' may not receive sensitive data",
        `attempts` empty.
    -   Exactly 2 rows (ids 41–42: `request.received`, `request.failed`),
        no `model.route.selected`, no `model.call.*`.
    -   Marker `SNARVIDEL`: 0 rows in events, 0 in Core's log.
    -   Core's log: no `Route chosen` or `Model call` line.
-   **C5, Secret refused with a key present:** pass.
    -   Secret, `allow_cloud` true: HTTP 503 `NoRoute`, "secret data is
        never sent to any model", `attempts` empty.
    -   Exactly 2 rows (ids 43–44), no `model.route.selected`, no
        `model.call.*`.
    -   Marker `TRUMBELVIX` (a fresh word; step 5's was already used): 0
        rows in events, 0 in Core's log.
    -   Core's log: no `Route chosen` or `Model call` line.
-   **C6, local down with cloud allowed:** skipped. It wasn't needed:
    the routing decision is covered by unit tests, and skipping it
    avoided an extra Claude call.
-   **Events table after the cloud half:** 14 rows, **ids 31–44**, all
    from the requests in both halves.
-   **Core's log:** no key-like string at any point.

**Measured cloud cost** (Sonnet 5.5: $2 input / $10 output per million
tokens):

-   C1: 266 input + 115 output tokens, about $0.002.
-   C3: tokens not recorded. Estimated from C1's size at about $0.02–$0.04;
    the exact figure is in the Anthropic console's usage page.
-   C4 and C5: $0. Neither request can reach the Claude API: routing
    refuses before any provider call, and the Claude client also refuses
    Sensitive and Secret data itself.
-   **Total for the cloud half:** about $0.02–$0.05.
-   At the time of this run there was no output token cap (16,000 per
    call), so the worst case was much higher: about $0.16 per call on
    Sonnet 5.5. #77 adds `AETHER_CLOUD_MAX_OUTPUT_TOKENS` (default 4096:
    about $0.04 per call, about $0.41 per 10-prompt run worst case).
