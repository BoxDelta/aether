# AETHER Technology Stack and Architecture Decisions

**Status:** Candidate stack; not yet validated as an integrated system

## 1. Selection principles

Choose technologies that: - Have active maintenance and clear
documentation. - Expose stable APIs or integration points. - Can be
replaced without rewriting the whole platform. - Fit the initial
hardware and budget. - Have acceptable licensing, privacy, and
data-handling terms. - Can be tested independently.

Avoid selecting tools solely because they are popular or because a demo
looks impressive.

## 2. Candidate stack (revised 2026-11-14)

  ---------------------------------------------------------------------------
  Area              Choice            Intended use          Status
  ----------------- ----------------- --------------------- -----------------
  Backend           Python 3 +        Core, router, memory  Accepted
                    FastAPI           API, tools            (ADR-017)

  Agent             Thin AETHER-owned Orchestration         Accepted
  orchestration     orchestrator                            (ADR-005);
                                                            OpenClaw after v1

  Local inference   Ollama            Selected local models Preferred initial
                                                            experiment

  Cloud reasoning   Claude API        Complex reasoning     Candidate; API
                                      and coding tasks      billing separate
                                                            from subscription

  Other model       Gemini, Groq,     Optional adapters /   Optional;
  providers         OpenRouter,       fallbacks             evaluate
                    Mistral, others                         individually

  Database          PostgreSQL        Memory, runtime       Accepted
                    (Docker)          tasks, events         (ADR-011)

  Semantic          pgvector inside   Similarity retrieval  Enable only when
  retrieval         the same Postgres                       retrieval tests
                                                            need it (ADR-011)

  Development       GitHub Issues /   Tracking the work of  Accepted
  task tracking     Projects          building AETHER       (ADR-010)

  Scheduling and    n8n (already      Scheduled jobs,       Accepted for v1
  simple workflows  self-hosted)      simple tool chains    (ADR-013)

  CI / Sentinel v1  GitHub Actions +  Tests, lint, type     Accepted
                    gitleaks          checks, secret scan   (ADR-018)

  Observability     Postgres events   Activity and Status   Accepted for v1;
                    table + web UI    views                 Langfuse optional
                                                            later

  Web UI            React (or         Command center, works Candidate
                    simpler) web app  on phone browsers

  3D visualization  Three.js          Neural memory graph   Future

  Remote access     Tailscale         Phone to home PC      Accepted for v1
                                                            (ADR-016)

  Android app       Kotlin + Jetpack  Native mobile client  Future
                    Compose                                 

  Version control   GitHub            Source, issues, CI    Accepted
  ---------------------------------------------------------------------------

Plane remains an option if GitHub Projects proves insufficient; it is no
longer the default because it adds another self-hosted service to run
and synchronize.

## 3. Local inference baseline

Hardware baseline: - One NVIDIA GPU with 8 GB VRAM. - 1 TB storage.

Initial constraints: - Do not plan around the older GPU; it is optional
future hardware. - Keep one local model loaded initially. - Start with
one parallel local request. - Evaluate compact quantized models, such as
a Qwen-family model, using real workload tests. - Evaluate
`nomic-embed-text` or another embedding model only when the memory
retrieval design requires it. - Monitor actual VRAM use and desktop
responsiveness. - Prefer cloud inference when local inference would
materially interfere with gaming or normal use. - Unload local models
when idle if that improves resource availability.

Potential Ollama settings such as one loaded model and one parallel
request are starting hypotheses, not guaranteed optimal values. Validate
them against the installed Ollama version and workload. Do not assume a
hard VRAM cap exists.

## 4. Provider-neutral interface

Nexus should expose a common internal contract independent of provider
SDKs. At minimum, the contract should represent: - Provider and model
identifier. - Supported input/output modalities. - Context and
capability metadata. - Request options. - Tool-call representation. -
Usage metadata where available. - Errors, timeouts, and retryability. -
Privacy classification and routing constraints.

Provider-specific adapters translate between this contract and vendor
APIs. Do not spread vendor-specific request formats throughout Core or
Memory.

**v1 implementation (`router/contract.py`, #13):** text-only messages,
a separate system prompt, request options, usage, and errors with a
`retryable` flag. Each provider declares `local` or `cloud` and the
highest privacy level it may receive (cloud: at most `personal`; no
provider ever receives `secret`). The shared `Provider.generate()`
enforces the privacy gate and the timeout, and converts unexpected
adapter exceptions into contract errors without copying their text.
Not yet represented: non-text modalities, tool calls, context-window
and capability metadata. Add them when a phase needs them.

Adapters: `router/ollama.py` (local, #14) and `router/claude.py`
(cloud, #15). A response's `finish_reason` is `stop`, `length`,
`refusal` (the model declined; no text is returned) or `other`. The
Claude adapter opts into Anthropic's server-side refusal fallback
(`fallbacks: "default"`), so a declined request is retried on
Anthropic's recommended fallback model inside the same call; the
response's `model` names whichever model answered.

## 5. Memory technology strategy

Start with structured storage and reliable metadata. Add vector search
only after real retrieval tests show it is useful.

A memory record should be able to represent: - Stable ID. - Content or
structured value. - Category. - Source/provenance. - Created and updated
timestamps. - Confidence or verification status where relevant. -
Sensitivity classification. - Retention and deletion state.

Embeddings are an index, not the source of truth. The original memory
record must remain authoritative.

## 6. Task tracking integration

Development work on AETHER is tracked in GitHub Issues and a GitHub
Project board (ADR-010). Suggested workflow:

-   Each accepted task is a GitHub Issue with acceptance criteria.
-   An issue moves to In Progress only when work actually starts.
-   Code changes go through a pull request, which triggers Sentinel.
-   Done requires merged code, passing checks, and met acceptance
    criteria.
-   Blocked issues carry a label and a written reason.

Runtime tasks that AETHER performs for the user live in AETHER's own
database. Do not synchronize the two without written conflict rules.

## 7. Architecture decision records

Create an ADR for each consequential choice. Each ADR should include: -
ID and title. - Status: Proposed, Accepted, Rejected, Superseded. -
Context and problem. - Options considered. - Decision and rationale. -
Consequences and risks. - Date and owners. - Revisit conditions.

### Initial decision register

  -----------------------------------------------------------------------
  ID                      Decision                Current status
  ----------------------- ----------------------- -----------------------
  ADR-001                 AETHER is a             Accepted project scope
                          cross-platform software 
                          platform, not an OS     

  ADR-002                 Start on Arch / GNOME   Accepted initial
                          with one 8 GB GPU only  baseline

  ADR-003                 Use a provider-neutral  Proposed
                          routing layer           

  ADR-004                 Keep shared memory      Accepted architectural
                          independent of model    principle
                          providers               

  ADR-005                 Thin AETHER-owned       Accepted
                          orchestrator; revisit   
                          OpenClaw after v1       

  ADR-006                 Evaluate Plane as task  Superseded by ADR-010
                          tracker                 

  ADR-007                 Build Neural from       Accepted architectural
                          observable events, not  principle
                          simulated activity      

  ADR-008                 Keep Sentinel           Accepted architectural
                          independent from code   principle
                          authoring               

  ADR-009                 Reduce v1 scope to      Accepted
                          Core, Nexus, Memory,    
                          web UI                  

  ADR-010                 GitHub Issues/Projects  Accepted
                          for development tasks   

  ADR-011                 One Postgres for        Accepted
                          memory, tasks, events   

  ADR-012                 Plain code names,       Accepted
                          branded doc names       

  ADR-013                 Use existing n8n for    Accepted
                          scheduling/workflows    

  ADR-014                 Merge Pulse into        Accepted
                          Neural Status view      

  ADR-015                 Vault starts as rules,  Accepted
                          not a service           

  ADR-016                 Mobile web over         Accepted
                          Tailscale as Android    
                          route                   

  ADR-017                 Python + FastAPI        Accepted
                          backend                 

  ADR-018                 Sentinel v1 in GitHub   Accepted
                          Actions                 

  ADR-019                 uv for Python tooling,  Accepted
                          mypy for type checks    

  ADR-020                 Postgres setup: Docker  Accepted
                          Compose, asyncpg,       
                          Alembic                 

  ADR-021                 How clients             Accepted
                          authenticate to Core    
                          (/chat)                 

  ADR-022                 Go public via a new     Accepted
                          repo from a clean       
                          snapshot                

  ADR-023                 Ollama is local only on Accepted
                          a loopback or private   
                          host                    
  -----------------------------------------------------------------------

## 8. Validation checklist before adopting a dependency

-   Is it actively maintained?
-   Is the license compatible with the intended use?
-   Does it support the required API/MCP integration?
-   What data leaves the machine?
-   Can it be self-hosted?
-   What are the actual costs and usage limits?
-   Can it be replaced?
-   Does it run on the target environment?
-   Does it introduce a new privileged execution path?
-   Can its failure be isolated without taking down Core?
