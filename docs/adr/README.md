# Architecture Decision Records

One file per consequential decision. Copy `0000-template.md`, number it,
and add it to the register below. This file is the decision register;
docs/03 points here.

ADR-001 to ADR-008 are the original baseline and are recorded only in
this register; ADR-005 also has its own file since it was accepted.
ADR-009 onward have their own files here.

## Register

-   **ADR-001:** AETHER is a cross-platform software platform, not an
    OS. Accepted project scope.
-   **ADR-002:** Start on Arch / GNOME with one 8 GB GPU only. Accepted
    initial baseline.
-   **ADR-003:** Use a provider-neutral routing layer. Proposed.
-   **ADR-004:** Keep shared memory independent of model providers.
    Accepted architectural principle.
-   **ADR-005:** Thin AETHER-owned orchestrator; revisit OpenClaw after
    v1. Accepted.
-   **ADR-006:** Evaluate Plane as task tracker. Superseded by ADR-010.
-   **ADR-007:** Build Neural from observable events, not simulated
    activity. Accepted architectural principle.
-   **ADR-008:** Keep Sentinel independent from code authoring. Accepted
    architectural principle.
-   **ADR-009:** Reduce v1 scope to Core, Nexus, Memory, web UI.
    Accepted.
-   **ADR-010:** GitHub Issues/Projects for development tasks. Accepted.
    Partly superseded by ADR-031 (Proposed).
-   **ADR-011:** One Postgres for memory, tasks, events. Accepted.
-   **ADR-012:** Plain code names, branded doc names. Accepted.
-   **ADR-013:** Use existing n8n for scheduling/workflows. Accepted.
-   **ADR-014:** Merge Pulse into Neural Status view. Accepted.
-   **ADR-015:** Vault starts as rules, not a service. Accepted.
-   **ADR-016:** Mobile web over Tailscale as Android route. Accepted.
-   **ADR-017:** Python + FastAPI backend. Accepted.
-   **ADR-018:** Sentinel v1 in GitHub Actions. Accepted.
-   **ADR-019:** uv for Python tooling, mypy for type checks. Accepted.
-   **ADR-020:** Postgres setup: Docker Compose, asyncpg, Alembic.
    Accepted.
-   **ADR-021:** How clients authenticate to Core (`/chat`). Accepted.
-   **ADR-022:** Go public via a new repo from a clean snapshot.
    Accepted (2026-11-14).
-   **ADR-023:** Ollama is local only on a loopback or private host.
    Accepted (2026-11-15).
-   **ADR-024:** Memory design for Phase 2. Accepted (owner,
    2026-11-15).
-   **ADR-025:** Database backups and restore. Accepted (owner,
    2026-11-16).
-   **ADR-026:** Encrypted volume for the Postgres data and the backups.
    Accepted (owner, 2026-11-27).
-   **ADR-027:** Telegram channel and Claude Code review workflow.
    Proposed (2026-11-26).
-   **ADR-028:** OpenRouter as an OpenAI-compatible provider adapter.
    Proposed (2026-11-26); build after Phase 2 exit criteria pass.
-   **ADR-029:** End-of-phase code-health review. Proposed (2026-11-26).
-   **ADR-030:** Dispatcher prototype during Phase 2. Accepted (owner,
    2026-11-26); amended 2026-11-27 (dry run ended, one always-Claude list,
    Claude path).
-   **ADR-031:** Plane as a one-way mirror of GitHub Issues; partly
    supersedes ADR-010. Proposed (2026-11-26).
-   **ADR-032:** An untrusted Ollama host needs `allow_cloud`, like the
    cloud (#22; amends ADR-023 §5). Accepted (owner, 2026-11-27).
-   **ADR-033:** How a model proposes memory (`propose_memory`; ADR-024
    D7, step 15). Accepted (owner, 2026-11-27).
-   **ADR-034:** Web UI stack: Vite + React + TypeScript in `web/`, built
    to static files that Core serves; an installable PWA. Accepted
    (owner, 2026-11-27).
-   **ADR-035:** Web and API authentication: Tailscale plus a single-user
    login with a session cookie, and scoped service tokens for machines.
    Accepted (owner, 2026-11-27).
-   **ADR-036:** Tool permission levels for v1 (docs/05 §4 per tool;
    High needs approval in the UI; no Critical tools). Accepted (owner,
    2026-11-27).
-   **ADR-037:** Live updates with Server-Sent Events. Accepted (owner,
    2026-11-27).
-   **ADR-038:** Agents as supervised clients of Core (registry, own
    token, pause and revoke). Accepted (owner, 2026-11-27).
-   **ADR-039:** One main chat with automatic topics. Accepted (owner,
    2026-11-27).
-   **ADR-040:** Local model via llama-swap (Qwen3.6-35B-A3B, direct
    OpenAI-compatible adapter, not through LiteLLM; ADR-030 fold-in).
    Accepted (owner, 2026-11-27).
-   **ADR-041:** Dispatcher identity: machine user `boxdelta-dispatch`
    with a classic `public_repo` token, timeline trust checks (#203), and
    one required code-owner approval via two rulesets, `checks` and `review` (#215).
    Accepted (owner, 2026-11-28).
-   **ADR-042:** Discord bot (step 4d): outbound only, claude route only,
    and an Ed25519 signature, made by a separate Linux user, that a request came from the owner.
    Accepted (owner, 2026-11-28).
