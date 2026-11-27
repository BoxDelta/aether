# AETHER Subsystem Reference

**Status:** Proposed naming and responsibility baseline

These names describe logical capabilities. They do not imply that each
subsystem must be a separate agent, executable, or server.
Implementation boundaries should follow operational and security needs.

## Release mapping and code names (revised 2026-11-14)

Branded names are used in docs and the UI. Code uses plain folder and
module names so developers and AI assistants can navigate easily
(ADR-012).

  ------------------------------------------------------------------------
  Subsystem          Code folder     Release   v1 form
  ------------------ --------------- --------- ---------------------------
  AETHER Core        `core/`         v1        Python service (proposed)

  AETHER Nexus       `router/`       v1        Library inside the backend

  AETHER Memory      `memory/`       v1        Postgres tables + API

  AETHER Automata    `tools/`        v1 (small) A few typed, permission-
                                               checked tools; n8n for
                                               simple workflows

  AETHER Connect     `web/`          v1        Responsive web UI over
                                               Tailscale

  AETHER Neural      `web/`          v1 (basic) Activity + Status views;
                                               3D graph is Future

  AETHER Pulse       `web/` + probe  v1 (basic) Status view inside Neural

  AETHER Sentinel    `.github/`      v1 (basic) GitHub Actions checks; AI
                                               review is Future

  AETHER Vault       rules           v1 (rules) `.env` + secret scan +
                                               redaction

  AETHER Chronos     n8n             Future    n8n until a native service
                                               is needed

  AETHER Oracle      ---             Future    Not started
  ------------------------------------------------------------------------

  -----------------------------------------------------------------------
  Subsystem               Primary role            Recommended
                                                  implementation form
  ----------------------- ----------------------- -----------------------
  AETHER Core             Central request         Agent/orchestrator
                          orchestration and       service
                          identity                

  AETHER Memory           Persistent shared       Database plus memory
                          context and retrieval   service

  AETHER Nexus            Provider adapters and   Routing service/library
                          model routing           

  AETHER Automata         Tool execution and      Tool service plus
                          workflows               controlled agents

  AETHER Sentinel         Independent quality and Deterministic engine
                          security review         plus reviewer agent

  AETHER Neural           Activity and memory     Web/desktop UI
                          visualization           

  AETHER Pulse            Health, performance,    Monitoring
                          and cost monitoring     service/dashboard

  AETHER Chronos          Scheduled and recurring Scheduler and job
                          work                    service

  AETHER Connect          Desktop, Android, and   Client applications and
                          remote client access    gateway

  AETHER Oracle           Research and source     Optional research
                          comparison              agent/workflow

  AETHER Vault            Secrets, credentials,   Security
                          and access policy       service/integration
  -----------------------------------------------------------------------

## AETHER Core

**Purpose:** Keep the platform coherent. Core receives requests,
coordinates subsystems, manages sessions, and presents results.

**Must own:** Request lifecycle, orchestration decisions, task
coordination, policy checks at workflow boundaries.

**Must not own:** Raw provider-specific API logic, unrestricted secret
storage, or direct unmediated host execution.

**Health and readiness (v1):**

-   `GET /health` is liveness only, and the one route without the API
    token (ADR-021).
-   `GET /ready` (#59, token required) runs three checks concurrently,
    each limited to 3 s, and returns one status word per component:
    -   `ollama`: `ok` when Ollama answers and the default model is
        pulled; `off_network` when it does but the host is untrusted, so
        it takes only requests with `allow_cloud` (ADR-032); otherwise
        `unavailable`, `not_configured`, `timeout` or `error`.
    -   `claude`: `configured` when a key is set. There's no network call,
        so this doesn't prove the key works. Otherwise `not_configured`,
        `timeout` or `error`.
    -   `database`: `ok` when it's reachable and migrated to head;
        `not_migrated`; or one of `core.db`'s categories
        (`not_configured`, `misconfigured`, `auth_failed`,
        `database_missing`, `unreachable`, `timeout`, `error`).
-   The overall status is one of:
    -   `ready` (200): everything is `ok` or `configured`.
    -   `degraded` (200): Ollama is fine, so default local-only requests
        work, but cloud isn't configured or events can't be stored.
    -   `not_ready` (503): Ollama isn't usable for default requests
        (including `off_network`).
-   The body never contains hosts, URLs, usernames, keys, model lists or
    exception text. A failed check logs only its component and error
    type.

## AETHER Memory

**Purpose:** Give the platform continuity across sessions and providers.

**Memory classes:** - Durable user-approved preferences. - Project
facts, plans, and architecture decisions. - Active task context. -
Searchable reference knowledge. - Operational metadata.

**Required controls:** Inspect, correct, export, and delete memory;
provenance; retention policy; backup and restore; sensitive-data
minimization.

**As built (Phase 2, ADR-024, #58).** The design is ADR-024; the
privacy rules are in `05` §7.1 and the backup rules in `05` §11.

-   **Store.** One Postgres table, `memory_records` (migration 0004):
    content, optional key, category (`profile`, `preference`, `project`,
    `decision`, `task`, `reference`), privacy level (never `secret`),
    status (`pending` or `approved`), provenance (source `owner`, `model`
    or `tool`, the provider and request ID, whether the level is the
    owner's or suggested), timestamps and an optional `expires_at`.
    Core writes it only through `memory/store.py`, and each change
    writes one content-free `memory.*` event in the same transaction.
    The only other writer is the restore repair (`memory/restore.py`),
    which runs by hand on a restored copy before it replaces the live
    database.
    Operational metadata stays in the events table, not in memory.
-   **Who decides.** The owner creates records directly (level
    required) and approves, rejects, corrects and deletes them. A model
    may only propose: with `propose_memory` on `/chat`, proposals are
    stored as `pending` and are never retrieved until the owner approves
    them with a level (ADR-033). At most 50 pending; a pending record
    expires after 30 days.
-   **Owner API.** `/memory` (Core, bearer token): list, get, search,
    create, approve, reject, correct (`PATCH`), delete and export.
    Commands are in the README.
-   **Retrieval.** Each model call, the failover call included, gets its
    own selection for the provider that receives it: approved, unexpired
    records within that provider's memory limit (Public and Personal by
    default, Sensitive only on a trusted Ollama host with the request's
    `include_sensitive`, nothing for the cloud without `allow_cloud`),
    keyed profile and preference records first, then full-text matches,
    at most 20 records and 4000 characters (lower for Ollama if
    `AETHER_OLLAMA_MEMORY_MAX_RECORDS` or `_MAX_CHARS` is set, for a
    small local model; #143). They go into the system
    prompt's memory section (`07` §4). Structured and full-text search
    only; pgvector is not used.
-   **Protection.** The database and its backups are in a LUKS2
    container file (ADR-026, since 2026-11-27); backups are verified,
    kept 7 daily plus 4 weekly, and the restore is tested, including the
    memory repair after a restore (`memory/restore.py`, runbook
    `docs/runbooks/db-backup-restore.md`).

## AETHER Nexus

**Purpose:** Choose the appropriate model for each request.

**Routing inputs:** Task type, quality needs, privacy classification,
cost, latency, provider health, quota, CPU/GPU/RAM load, and user
preferences.

**Initial model direction:** Claude for difficult reasoning/coding when
appropriate; Ollama for selected local/private tasks; other providers
remain optional adapters.

**Hardware policy:** Begin with the single 8 GB VRAM GPU only. Protect
gaming and normal desktop responsiveness. Do not assume a model fits or
performs acceptably until tested on the actual system.

**Hardware probe (`router/hardware.py`, #17):** Reports free VRAM, GPU
utilization (GPU 0) and available RAM. Uses NVML (`nvidia-ml-py`), falls
back to parsing `nvidia-smi`, else reports "unknown".
`has_local_headroom()` is true only when free VRAM is at least the Charter
reserve of 1.5 GiB. Unknown GPU state counts as no headroom, so routing
prefers cloud unless privacy or a manual override says otherwise. Each
result includes a loggable reason. The free VRAM figure already accounts
for any model Ollama has loaded. It does not predict how much a model
that isn't loaded yet will need.

**Routing rules (`router/routing.py`, #18):** Each request carries
`RouteOptions`: `allow_cloud` (default **false**) and an optional manual
`override` (a provider name). Rules, in order:

1. Secret data is refused before any probe or provider call.
2. An override picks that provider only if the request allows it. Cloud
   needs `allow_cloud`, and the provider must accept the privacy level. It
   must also be available. Otherwise the request is refused; no other
   provider is substituted. An override skips the VRAM headroom check, and
   the reason says so.
3. Local, if available and the probe shows VRAM headroom.
4. Cloud, if `allow_cloud` is set, the privacy level allows it (Public or
   Personal), and it is available.
5. Local without headroom, if cloud isn't an option. The request isn't
   lost, and the reason records that the desktop reserve wasn't met.
6. Otherwise `NoRoute`, with the reason.

Every decision is logged as `Route chosen` or `No route`, with the reason
and no prompt text. The probe runs in a worker thread
(`asyncio.to_thread`). Routing calls providers only through
`call_with_identity`. `tests/test_generate_guard.py` scans the repository
with `ast` and reports each violation as file:line. It allows `.generate`
only in `router/identity_call.py`. It allows `._generate` only in
`router/contract.py`, because calling it directly skips the privacy check,
timeout and error scrubbing. It allows `anthropic` imports only in
`router/claude.py` and `ollama` imports only in `router/ollama.py`.
Sensitive data never goes to cloud. Per-request owner approval for
Sensitive → cloud (docs/05 §7.1) isn't implemented yet.

**Failover (`Router.handle`, #19):** If the call fails with a
`ProviderError` whose `retryable` is true (timeout, connection failure,
rate limit, server error or overload), routing runs `decide()` again
without the failed provider. It makes at most one fallback call. That
means all the rules above apply again: cloud needs `allow_cloud`,
Sensitive stays local, and the fallback must be available. Failover
doesn't happen in these cases:

- the error isn't retryable (no API key, authentication, model not found,
  rejected request, bad or empty response, adapter bug, privacy refusal);
- the request used a manual override;
- the model returned any response, including a refusal
  (`finish_reason="refusal"`). A refusal is the final answer.

Any failure after a call raises `RouteFailed`, with each attempt's provider,
error type and scrubbed message, so the request is never dropped silently.
Each decision is logged as `Failover`, `No failover` or `Failover failed`.
The logs carry the reason and the scrubbed error text, never the prompt.

## AETHER Automata

**Purpose:** Turn approved plans into controlled actions.

**Examples:** Approved file operations, browser tasks, project-tracker
updates, local development workflows, and notifications.

**Controls:** Typed tools, scoped permissions, confirmation for
consequential actions, execution logs, timeouts, cancellation, and
isolation for risky tasks.

## AETHER Sentinel

**Purpose:** Independently evaluate software changes and protect
quality.

**Review layers:** 1. Formatting, lint, and type checks. 2. Automated
tests. 3. Secret and static security scanning. 4. Dependency and
supply-chain scanning. 5. Independent AI review (Future in CI; today
manual and advisory, outside Sentinel: docs/05 §10). 6. Approval gates
and recovery records.

**Rule:** AI reviewer agreement is not proof of correctness. Findings
must cite evidence, severity, confidence, and test coverage.

## AETHER Neural

**Purpose:** Make system activity understandable.

**Activity mode:** Actual requests, routing, memory retrieval, tool
calls, task updates, Sentinel checks, and outcomes.

**Memory mode:** Stored memories, projects, concepts, and relationships
with provenance.

**Rule:** No fabricated activity and no representation of hidden
chain-of-thought as observable data.

## AETHER Pulse

**Purpose:** Show whether AETHER is healthy and what it costs.

**Metrics:** Service uptime, request latency, errors, queue depth,
provider usage and estimated cost, quota warnings, and local resource
load.

## AETHER Chronos

**Purpose:** Schedule work and recurring checks.

**Requirements:** Time zone awareness, missed-run behavior, retry
policy, cancellation, audit history, and permission checks at execution
time.

## AETHER Connect

**Purpose:** Provide consistent access from supported clients.

**Initial direction:** Existing Android companion or secure web access
first; custom Android application later if it provides meaningful value.

## AETHER Oracle (optional)

**Purpose:** Research external information, compare sources, and
preserve citations.

**Rule:** Distinguish source-backed findings from inference and
uncertainty.

## AETHER Vault

**Purpose:** Protect credentials and sensitive configuration.

**Requirements:** Use established secret-management mechanisms,
least-privilege access, rotation where supported, audit trails, and
redaction. Never commit secrets to Git.
