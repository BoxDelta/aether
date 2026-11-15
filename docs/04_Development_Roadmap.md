# AETHER Development Roadmap

**Status:** Revised sequence (2026-11-14, see ADR-009)\
**Planning principle:** Build a reliable vertical slice that is used
every day before expanding the platform.

The original eight-phase plan was sized for a team. This roadmap has four
phases for v1 and a Future list. Future items remain part of AETHER's
vision; they are scheduled once v1 is in daily use.

## Phase 0 --- Repository foundation and Sentinel v1

**Goal:** A safe, documented place to build AETHER.

Deliverables:

-   Private GitHub repository with `docs/`, `docs/adr/`, `.gitignore`,
    `.env.example`, `README.md` and `CLAUDE.md`.
-   Sentinel v1 in GitHub Actions: secret scan (gitleaks) on every push.
-   GitHub Project board; Issues for Phase 1 work.
-   ADRs for decisions made so far; backend language decided (ADR-017).
-   Drafts of the identity specification and privacy classification.
-   Branch protection on `main` (checks must pass) once Phase 1 adds
    tests. **Not enforceable on GitHub Free while the repository is private** (ADR-022 enables it after the public flip);
    the owner merges with a `safemerge` function that waits for checks
    instead (ADR-018, #10).

Exit criteria:

-   A developer or AI assistant can start Phase 1 without guessing core
    requirements.
-   Sentinel runs green on `main`.
-   No credentials or personal memory data are committed.

## Phase 1 --- Core and Nexus (one identity, two models)

**Goal:** One request path, one identity, local and cloud models.

Deliverables:

-   Minimal Core service with a health endpoint, request/session IDs,
    configuration from environment, structured logging.
-   AETHER identity loaded from a versioned file and applied to every
    model call.
-   Provider-neutral model contract (`router/`).
-   Claude API adapter and Ollama adapter.
-   Simple, explainable routing rules plus a manual override.
-   GPU/RAM probe so routing can avoid local inference under load.
-   `events` table recording requests, routes, errors.
-   Sentinel extended: tests, lint, type checks on every push.

Exit criteria:

-   A request can be routed to either local or cloud inference, and the
    routing reason is logged.
-   Provider failure or timeout is visible and does not lose the request.
-   Local inference meets the desktop-responsiveness and VRAM targets in
    the Charter on the real PC.
-   Tests pass in Sentinel.

## Phase 2 --- Memory

**Goal:** Cross-provider continuity, the core of AETHER.

Deliverables:

-   Memory schema in Postgres (record fields per `03` section 5).
-   Privacy classification applied to every record.
-   User-approved create/update flow.
-   Retrieval API (structured first; pgvector only if tests show need).
-   Inspect, correct, export and delete controls.
-   Backup and tested restore.

Exit criteria:

-   A fact saved while one provider handled a request is retrieved when
    the other provider handles a later request.
-   Context above a provider's allowed privacy level is never sent to
    it (tested).
-   Deletion and restore are tested.

## Phase 3 --- Web UI, tools and daily use

**Goal:** Make AETHER usable every day from the PC and the phone.

Deliverables:

-   Responsive web UI: chat, memory review, Activity view, Status view
    (Neural/Pulse basics).
-   Tailscale access from the Android phone.
-   Small tool set with permission levels from `05`, approvals in the UI.
-   n8n integration for scheduled jobs and simple workflows, calling
    Core's authenticated API.
-   Cancel and inspect running work.

Exit criteria:

-   AETHER is used daily from desktop and phone.
-   Tools cannot bypass permission enforcement (tested).
-   Activity view shows only recorded events.

## Future (after v1 is in daily use)

Not ordered; pick based on what daily use shows is most valuable.

-   Neural 3D activity and memory graph (Three.js).
-   Sentinel AI review layer (independent reviewer on pull requests).
-   Native Chronos scheduling service, if n8n becomes limiting.
-   Oracle research workflows with citations.
-   Native Android app (Kotlin + Jetpack Compose).
-   Spoken voice for Aether (speech-to-text and text-to-speech).
-   Dedicated Vault secrets service.
-   Additional provider adapters (Gemini, Groq, OpenRouter, Mistral).
-   Hosted always-on gateway with the home PC as a worker.
-   Threat-model review, load/failure testing, backup drills.

## Prioritization rules

1.  Security and data integrity precede convenience features.
2.  Core, Nexus and Memory precede sophisticated visualization.
3.  Track AETHER's own development in GitHub from Phase 0.
4.  Sentinel's deterministic checks precede any AI reviewers.
5.  Build only the minimum interfaces each phase needs.
6.  Do not implement every named subsystem as a separate agent or
    service by default.
