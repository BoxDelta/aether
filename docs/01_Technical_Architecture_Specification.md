# AETHER Technical Architecture Specification

**Status:** Draft architecture baseline\
**Version:** 0.2\
**Date:** 2026-11-14

> **Revision 0.2:** Reduced v1 scope (ADR-009), Pulse merged into Neural
> (ADR-014), Vault starts as a rule set (ADR-015), one Postgres database
> for memory and events (ADR-011), n8n for early scheduling and simple
> workflows (ADR-013), mobile web UI as the Android route (ADR-016).

## 1. Purpose

This document defines a proposed architecture for AETHER. It establishes
component responsibilities, trust boundaries, data flows, and
implementation constraints. It is a starting specification, not a claim
that the system currently exists.

## 2. Architectural overview

AETHER should be implemented as a modular platform with a central
orchestration service and independently testable supporting services.

``` text
                 Desktop / Android / Web UI
                            |
                     AETHER Connect
                            |
                       AETHER Core
                  /         |          \
             AETHER       AETHER      AETHER
              Nexus       Memory     Automata
               |            |           |
        Model Providers  Memory DB   Tool Adapters
        /     |     \                  /   |   \
     Claude Gemini Ollama          Files Browser Desktop
               |
        AETHER event stream (Postgres events table)
            /                 \
   Neural (incl. status /       Chronos
   Pulse view)                  (n8n at first)

   Sentinel (independent review system) runs in GitHub Actions,
   outside the runtime, and checks every change to AETHER itself.
```

The diagram is conceptual. It does not prescribe that every box must run
as a separate process. Start with the simplest deployment that preserves
clear module boundaries.

## 3. Core architectural rules

1.  Core coordinates requests but should not directly implement every
    subsystem's storage or provider-specific logic.
2.  Memory is provider-independent. Models receive selected context
    through a controlled retrieval interface.
3.  Nexus owns model selection and provider adapters.
4.  Automata owns tool execution and enforces permission checks before
    actions.
5.  Sentinel is logically independent from the code authoring workflow
    and maintains its own findings and review records.
6.  Neural visualizes recorded events and stored relationships; it does
    not fabricate activity or expose hidden model reasoning.
7.  Chronos schedules jobs, while Core or a task worker executes them.
8.  Connect is a client/access layer, not a second source of truth.
9.  All consequential actions must be auditable.
10. Subsystems communicate through versioned interfaces and explicit
    event contracts.

## 4. Component responsibilities

### 4.1 AETHER Core --- orchestration

Responsibilities: - Accept user requests from supported clients. -
Resolve identity, session, permissions, and relevant project context. -
Decide whether a request needs a direct response, model call, tool
workflow, or background task. - Coordinate Nexus, Memory, Automata, and
Sentinel. - Maintain task lifecycle and communicate status. - Handle
cancellation, timeouts, retries, and partial failures.

Core must not bypass permission enforcement or directly expose
unrestricted host access to a model.

### 4.2 AETHER Memory --- shared memory service

Responsibilities: - Store user-approved preferences, facts, project
context, decisions, and task-related context. - Retrieve relevant
context for a request. - Track provenance, timestamps, confidence, and
source where appropriate. - Support review, correction, export, and
deletion workflows. - Separate durable memory from transient
conversation context and raw logs.

Proposed logical data categories: - Profile/preferences (non-sensitive
and user-approved). - Project knowledge and architecture decisions. -
Active task and workflow context. - Reference documents and indexed
knowledge. - Audit metadata.

Memory should not indiscriminately ingest every conversation or file.
Sensitive information should be excluded by default unless a clear,
authorized need exists.

### 4.3 AETHER Nexus --- provider routing

Responsibilities: - Present a provider-neutral model interface. -
Maintain provider capabilities, availability, limits, and
configuration. - Select a model based on task class, quality
requirements, privacy, latency, cost, quota, and system resource load. -
Support fallback when a provider is unavailable. - Record the selected
provider and routing reason.

Routing must be explainable and configurable. Do not send sensitive
context to a cloud provider merely because it is cheaper or faster.

Local inference is llama-swap (ADR-040), an OpenAI-compatible server that
Nexus calls directly on loopback, never through LiteLLM or Langfuse, with
thinking off on every request. The Ollama adapter stays but is disabled
by default. Whether the local model is loaded comes from llama-swap's own
`/running` endpoint; a loaded model needs no new VRAM, so it never blocks
routing to itself, and a cold one needs its size plus the reserve free. Host trust and privacy rules: docs/05 §7.1.

### 4.4 AETHER Automata --- tools and execution

Responsibilities: - Provide typed, permission-checked tool adapters. -
Execute bounded tasks such as approved file operations, browser
workflows, and desktop actions. - Capture execution output, errors, and
evidence. - Support cancellation and time limits. - Run risky operations
in isolated environments where feasible.

A model may propose an action; Automata decides whether that action is
permitted under the current policy.

### 4.5 AETHER Sentinel --- quality and security

Responsibilities: - Run deterministic checks such as tests, linting,
type checks, secret scanning, dependency scanning, and static
analysis. - Consolidate findings with evidence, severity, and
confidence. - Preserve review history and approval decisions. - Require
human approval for critical changes.

**Sentinel v1** is deterministic checks in GitHub Actions only: tests,
lint, type checks and the secret scan; CodeQL also runs on pull
requests. AI review is not part of Sentinel today. It is manual and
advisory (the `security-reviewer` subagent, `@codex review` on request),
and an automated AI reviewer in CI stays Future (docs/04, docs/05 §10).

Sentinel must not be able to silently disable its own required checks or
approve protected changes solely on its own authority.

### 4.6 AETHER Neural --- observability interface

Responsibilities: - Visualize actual events, task progress, provider
activity, memory retrieval, tool execution, and subsystem health. -
Provide a persistent memory/project graph where supported by stored
data. - Allow users to inspect event details and provenance.

Neural may display observable actions and status, but must not claim to
show private chain-of-thought or hidden internal reasoning.

### 4.7 AETHER Pulse --- health and resource monitoring

**v1 form:** Pulse is the "Status" view inside Neural plus a small
resource probe that reports GPU/CPU/RAM pressure to Nexus. It is not a
separate service until there is a clear need (ADR-014).

Responsibilities: - Track service health, latency, error rates, queue
depth, resource use, and estimated provider costs. - Report GPU/CPU/RAM
pressure to Nexus. - Alert on failed services, unusual usage, and quota
limits.

### 4.8 AETHER Chronos --- scheduling

**v1 form:** Scheduled and simple multi-step workflows run in the
existing self-hosted n8n instance, calling AETHER Core through its
authenticated API. A native Chronos service is a Future item (ADR-013).

Responsibilities: - Store scheduled and recurring jobs. - Trigger jobs
at defined times or intervals. - Track job status, retries, missed runs,
and cancellation. - Apply the same permission rules as interactive
tasks.

### 4.9 AETHER Connect --- client connectivity

Responsibilities: - Provide authenticated access from desktop, Android,
and supported web clients. - Keep clients synchronized with the central
backend. - Deliver notifications and task updates. - Avoid storing
authoritative copies of memory or task state on clients.

**v1 form:** A responsive web UI served by the home PC and reached from
the Android phone over Tailscale. A native Android app is a Future item
(ADR-016).

### 4.10 AETHER Oracle --- research

Optional subsystem: - Gather information from approved sources. -
Compare claims and preserve source references. - Distinguish verified
facts, uncertain claims, and model-generated interpretation.

### 4.11 AETHER Vault --- secrets and policy

Security-critical subsystem: - Store or broker credentials using an
established secrets-management approach. - Enforce access policies and
scoped credentials. - Record access events. - Prevent secrets from
appearing in prompts, logs, source control, or task descriptions.

**v1 form:** Vault is a set of enforced rules, not a service: secrets
live only in a git-ignored `.env` (or the OS keyring), `.env.example`
documents the names, Sentinel's secret scan runs on every push, and
logging redacts known secret values. A dedicated secrets service is a
Future item (ADR-015).

## 5. Main request lifecycle

1.  Client sends a request to Core over an authenticated channel.
2.  Core identifies the user/session and evaluates permissions.
3.  Memory retrieves only context relevant to the request and permitted
    for the selected processing path.
4.  Core asks Nexus to select a provider or model.
5.  Nexus returns a model route and the reason for selection.
6.  The model may respond directly or propose tool actions.
7.  Automata validates permissions and executes approved tools.
8.  Core records events and updates task status.
9.  If the task changes code or another protected artifact, Sentinel
    runs the required checks.
10. Core returns a result with status, evidence, limitations, and any
    approval request.
11. Memory may propose a durable update; user-controlled policy
    determines whether it is stored.
12. Neural (including its Status/Pulse view) reads the event store for
    visualization and monitoring.

## 6. Task lifecycle and status semantics

Recommended baseline statuses: - **Backlog:** captured but not
scheduled. - **To Do:** accepted and ready to be worked. - **In
Progress:** execution has actually started. - **Blocked:** cannot
proceed; blocker must be recorded. - **Review:** work is awaiting tests,
Sentinel, or human review. - **Done:** acceptance criteria are satisfied
and required checks passed. - **Cancelled:** intentionally stopped, with
reason recorded.

An agent must not mark a task In Progress before it begins, or Done
merely because it generated a completion message. Status changes should
be driven by execution events and acceptance criteria.

## 7. Event architecture

AETHER should produce structured events for meaningful system activity.
Example event types: - `request.received` - `model.route.selected` -
`memory.context.retrieved` - `tool.execution.started` -
`tool.execution.completed` - `tool.execution.failed` -
`task.status.changed` - `sentinel.review.started` -
`sentinel.finding.created` - `approval.requested` - `approval.granted` -
`approval.denied` - `provider.unavailable` - `chat.topic.created` -
`chat.message.routed` - `chat.topic.merged` - `chat.topic.archived`
(the `chat.*` types are planned for Phase 3, ADR-039)

Each event should include, where applicable: - Unique event ID. -
Timestamp. - Correlation/request ID. - Task ID. - Actor or subsystem. -
Event type and schema version. - Outcome and safe metadata. - References
to evidence or logs.

Do not put API keys, passwords, access tokens, or unnecessary personal
content in events.

**v1 transport and storage:** Events are rows in an append-only `events`
table in the same Postgres database as Memory (ADR-011). Neural reads
from it; no message broker is introduced until a measured need exists.

**v1 table (`events`, migration 0002, `core/events.py`, #21):** Metadata
only. Every column is typed and has a CHECK constraint, and there is no
free-form JSON field, so no column can hold prompt text, answer text,
memory content, secrets or tracebacks. A trigger refuses `UPDATE`,
`DELETE` and `TRUNCATE`. Event types: `request.received`,
`model.route.selected`, `model.call.completed`, `provider.unavailable`,
`request.failed`. Provider errors store only the error type and the
scrubbed `ProviderError.message`, never `__cause__`/`__context__` or a
traceback. When an event is written:

- every free-text field (route reason, error message, attempt messages)
  goes through the log redactor;
- a field that contains any of the request's own text is replaced with
  `[REMOVED: contained request text]`.

Columns: `id`, `occurred_at`, `recorded_at`, `event_type`,
`schema_version`, `actor`, `outcome`, `request_id`, `session_id`,
`privacy`, `allow_cloud`, `include_sensitive` (migration 0008), `override`, `provider`, `provider_kind`, `model`,
`route_reason`, `finish_reason`, `latency_ms`, `input_tokens`,
`output_tokens`, `error_type`, `error_message`, `attempts` (array of at
most two `{provider, error_type, message, retryable}`).

**Memory events (migration 0005, ADR-024 §5, #72):** event types
`memory.proposed`, `memory.approved`, `memory.rejected`,
`memory.updated`, `memory.deleted`, `memory.expired`, `memory.exported`
and `memory.context.retrieved`, with actor `memory` (or `restore`, for
the restore runbook, ADR-024 §4); every other event keeps `core` or
`router`. Typed columns, each with a CHECK, and no JSON:

- record events (proposed, approved, rejected, updated, deleted,
  expired): `memory_record_id`, `memory_category`, `memory_source` and
  `memory_level`, all required; `memory_old_level` on `memory.updated`
  only, when the level changed;
- `memory.context.retrieved`: `request_id`, `provider`,
  `memory_count_public`, `memory_count_personal`,
  `memory_count_sensitive`, `memory_chars`, `memory_effective_level`,
  `memory_record_ids` (at most 20 UUIDs, one per record sent) and
  `memory_left_out`, all required;
- `memory.exported`: `memory_export_count`.

Each column is NULL on every other event type. Level columns never
allow `secret`. Never recorded: memory content, keys, corrected text,
search queries or rejection reasons. A memory event is written in the
same transaction as the change it records (`core.events.write_memory_event`).
The memory store writes proposed, approved, updated and deleted today;
the other four come with later ADR-024 steps.

## 8. Data ownership

  -----------------------------------------------------------------------
  Data                                Authoritative owner
  ----------------------------------- -----------------------------------
  User-approved durable memory        AETHER Memory

  Provider configuration and routing  AETHER Nexus / Vault
  policy                              

  Development task state (building    GitHub Issues / Projects
  AETHER itself)                      

  Runtime task and job state          AETHER Core (Postgres)

  Tool execution records              Automata / event store

  Code review findings                Sentinel (GitHub Actions results
                                      and PR reviews)

  System metrics                      Neural Status view / event store

  Scheduled job definitions           n8n at first, Chronos later

  Client preferences and local UI     Connect client, where appropriate
  state                               
  -----------------------------------------------------------------------

GitHub Issues/Projects is used for development tasks (ADR-010). Runtime
tasks stay in AETHER's own database. Do not add two-way synchronization
between them without written conflict rules.

## 9. Deployment model

### Initial deployment

-   Run the initial backend on the Arch / GNOME PC.
-   Use Ollama for local inference experiments.
-   Use a cloud provider for tasks that benefit from it.
-   Keep the desktop responsive; local inference is optional and
    resource-aware.
-   Use authenticated local connectivity for clients; Tailscale for the
    phone.
-   One PostgreSQL instance (Docker) holds memory, runtime tasks and
    events; pgvector is enabled in it only when retrieval tests need it.

### Later deployment option

A hosted gateway and memory service may be considered for always-on
access. The home PC can remain a secure worker for local inference and
desktop automation. This requires secure networking, authentication,
backup, monitoring, and cost review.

## 10. Reliability requirements

-   Every background task has a durable ID and state.
-   Long-running work supports cancellation and timeout.
-   Retries are bounded and safe; non-idempotent actions require special
    handling.
-   Provider failures are visible and do not silently lose task state.
-   Event records support debugging and audit.
-   Backups and restore procedures are tested.
-   Partial completion is reported honestly.

## 11. Open architecture decisions

Resolved: backend (Python + FastAPI, ADR-017), orchestration (thin
AETHER-owned orchestrator, ADR-005), identity name ("Aether"),
task tracker (GitHub, ADR-010), database and
vector approach (one Postgres + pgvector later, ADR-011), event
persistence (Postgres events table, ADR-011), Android strategy (mobile
web over Tailscale, ADR-016), monthly cloud API cost ceiling ($5 per
month, set in the Anthropic console, docs/00), web UI stack (ADR-034),
web and API authentication (ADR-035), tool permission levels (ADR-036),
live updates (SSE, ADR-037), agents as supervised clients (ADR-038).

Still open and must be recorded when resolved:

-   Desktop automation framework and isolation model.
-   Hosted versus home-hosted deployment timeline.
-   Provider choices beyond Claude and Ollama (OpenRouter: ADR-028,
    Proposed) and their API privacy terms.

## 12. Acceptance criteria for architecture baseline

The architecture is ready to guide implementation when: - Every
subsystem has a bounded responsibility. - Core interfaces and event
contracts are documented. - Data ownership and synchronization rules are
clear. - Permission enforcement points are identified. - Failure and
recovery behavior is defined for critical workflows. - Open decisions
are tracked rather than hidden.
