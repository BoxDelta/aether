# AETHER Developer Handoff

This document is intended for a developer or AI coding assistant helping
implement AETHER.

## 1. Project summary

AETHER is a cross-platform personal AI platform with one persistent
identity, shared memory, model-provider routing, controlled automation,
task tracking, independent software review, and desktop/Android access.

Initial host: Arch / GNOME.\
Initial GPU: NVIDIA, 8 GB VRAM.\
Storage: 1 TB.\
Shell: Fish.\
AI coding assistant: Claude Code, working from `CLAUDE.md`.

The optional older GPU is not part of the initial implementation plan. Do
not design the first release around multi-GPU inference.

## 2. Key requirements

-   AETHER owns the shared memory; providers do not share native
    memories automatically.
-   Nexus routes requests between local and cloud providers.
-   Local inference must respect resource limits and desktop
    responsiveness.
-   Automata executes only authorized tools.
-   Sentinel independently checks software changes.
-   Neural visualizes actual recorded activity and memory relationships.
-   Task tracking must reflect real work state.
-   Android connects to the same backend identity and memory.
-   Security, privacy, and recovery are first-class requirements.

## 3. Repository structure (revised 2026-11-14, ADR-012)

Code uses plain names; the branded subsystem names stay in docs and UI.

``` text
aether/
├── CLAUDE.md            # rules for AI coding assistants (read first)
├── README.md
├── .env.example         # names of required secrets, no values
├── .gitignore
├── .github/workflows/   # Sentinel (GitHub Actions)
├── docs/
│   ├── 00_ ... 07_*.md  # specification
│   └── adr/             # architecture decision records
├── core/                # AETHER Core: API, sessions, orchestration
├── router/              # AETHER Nexus: provider contract + adapters
├── memory/              # AETHER Memory: schema, retrieval, controls
├── tools/               # AETHER Automata: typed, permission-checked tools
├── web/                 # Connect + Neural + Status (Pulse) UI
├── identity/            # versioned AETHER identity definition
└── tests/
```

Create folders only when the phase that needs them starts.

## 4. Development rules

1.  Read the documentation before proposing structural changes.
2.  Do not silently change product scope or architecture.
3.  Record major technical decisions as ADRs.
4.  Prefer small, testable changes.
5.  Add tests with behavior changes.
6.  Never commit API keys, tokens, private memory, or user data.
7.  Do not run destructive commands without explaining their impact and
    receiving authorization.
8.  Do not claim tests passed unless they were actually run and their
    result was observed.
9.  Do not claim a feature is complete when only a mockup or stub
    exists.
10. Document known limitations and unfinished work.

## 5. First implementation recommendation

Follow `04_Development_Roadmap.md`. In short:

1.  Phase 0: repository, docs, ADRs, Sentinel secret scan. Done
    (2026-11-14).
2.  Phase 1: FastAPI Core with health endpoint and request/session IDs,
    identity file, Claude adapter, Ollama adapter, routing with logged
    reasons, `events` table, tests running in Sentinel. Done (exit check
    passed 2026-11-14).
3.  Phase 2: Memory. In progress (build order: ADR-024 §9, #58).
4.  Phase 3: web UI, phone access, tools, n8n. Not started.

The status line under each phase in `04` is the source of truth.

Do not begin by implementing all subsystems, a complex 3D interface, or
multiple autonomous agents simultaneously.

## 6. Questions to resolve before coding

Resolved: task tracker (GitHub, ADR-010); database (Postgres, ADR-011);
Android route (mobile web, ADR-016); backend (Python + FastAPI,
ADR-017); Python tooling (uv + mypy, ADR-019); orchestration (thin
AETHER-owned orchestrator, ADR-005); identity name and default tone (`07`);
privacy levels (`05` section 7.1, confirmed 2026-11-26); monthly cloud
API cost ceiling ($5 per month in the Anthropic console, `00`).

Still open:

-   What tool actions are allowed without confirmation?
-   What should continue working when the home PC is off?

## 7. Definition of an acceptable implementation contribution

A contribution should include: - A clear problem statement. - A scoped
implementation. - Tests or a reason tests are not applicable. - Security
and privacy impact. - Documentation updates where needed. - Actual test
results. - Known limitations and rollback notes for risky changes.
