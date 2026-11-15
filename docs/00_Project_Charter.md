# AETHER Project Charter

**Status:** Draft baseline\
**Product classification:** Cross-platform personal AI platform\
**Working tagline:** One identity. Many minds. Shared memory.

## 1. Vision

AETHER aims to provide one coherent personal AI experience across
devices and model providers. Rather than treating each AI provider as a
separate assistant with isolated context, AETHER coordinates models
through a shared identity, user-controlled memory, task system, and tool
layer.

The user should be able to move between local and cloud models without
losing the continuity of the assistant or the context of ongoing
projects.

## 2. Product definition

AETHER is a modular personal AI platform composed of: - A central
orchestration layer that receives requests and coordinates work. - A
model-provider layer that selects suitable local or cloud models. - A
shared memory service that stores and retrieves approved context. - A
controlled tool and automation layer. - A task and background-work
system. - Independent quality and security review capabilities. -
Desktop and Android access. - A visual interface for understanding
activity, memory, and system health.

AETHER is not intended to train a foundation model from scratch. It
integrates existing models and software components behind a consistent
platform.

## 3. Product goals

1.  **Continuity:** Maintain one coherent assistant identity across
    model providers and devices.
2.  **Shared memory:** Make relevant, user-approved context available to
    different providers through AETHER.
3.  **Intelligent routing:** Choose a model based on task suitability,
    privacy, latency, cost, availability, and local resource load.
4.  **Useful action:** Allow the agent to use approved tools and perform
    bounded tasks.
5.  **Observability:** Make agent activity, tool calls, task status, and
    system health understandable to the user.
6.  **Reliability:** Handle provider failures, interrupted jobs, and
    recoverable errors without silently losing work.
7.  **Security:** Keep credentials protected, minimize data sharing, and
    require approval for consequential actions.
8.  **Cost awareness:** Prefer cost-effective solutions without
    compromising necessary quality or reliability.

## 4. Non-goals and exclusions

The initial project does not include: - Building a new operating system
or Linux distribution. - Training a general-purpose foundation model
from scratch. - Giving an AI unrestricted access to the host computer. -
Treating AI-generated claims as proof that a task succeeded. -
Automatically sending all personal memory to every cloud provider. -
Requiring every proposed subsystem to be a separate process or agent. -
Supporting multiple GPUs in the first implementation.

## 5. Target platforms

### Initial host

Arch / GNOME on the user's existing PC. Initial desktop automation and
local inference will be developed and tested here.

### Mobile access

Android access should initially use an existing supported client or a
secure web interface where practical. A custom Android application may
follow once the backend interfaces stabilize.

### Remote availability

The first deployment may run on the home PC. A later hosted gateway can
be evaluated if continuous availability while the PC is powered off
becomes a firm requirement. This decision must account for recurring
hosting cost, security, and operational complexity.

## 6. Guiding principles

-   Modular, documented interfaces over tightly coupled components.
-   One central identity and shared context, not competing assistant
    personalities.
-   Human control over sensitive data and consequential actions.
-   Observable execution: report what actually happened, not what the
    model intended.
-   Least privilege for tools and agents.
-   Local inference when it is useful and does not unacceptably affect
    PC responsiveness.
-   Cloud inference when it offers a better balance of quality, speed,
    privacy, and cost.
-   Replaceable providers and integrations; avoid unnecessary vendor
    lock-in.
-   Build incrementally and validate each stage.

## 7. Release scope (revised 2026-11-14, see ADR-009)

AETHER is built by one person, so the plan is split into a small first
release and a "Future" list. Future items are still part of the vision;
they are sequenced after the core proves useful, not dropped.

**First release (v1):** AETHER Core, AETHER Nexus (model routing),
AETHER Memory, and a simple web UI that also works on a phone.

**Future (not scheduled into numbered phases yet):** Neural 3D
activity/memory graph, full Chronos scheduling service, Oracle research
workflows, a custom Android app, AI review in Sentinel, a dedicated Vault
service, and a hosted always-on gateway.

## 8. Initial success criteria

AETHER's first meaningful release should demonstrate:

-   One entry point for requests, with one defined AETHER identity (see
    `07_Identity_Specification.md`).
-   At least one cloud model and one local Ollama model connected
    through a common provider interface.
-   A shared memory workflow that can retrieve approved context across
    provider changes.
-   Logs/events showing model selection, routing reason, tool
    execution, and outcomes.
-   A small set of safe, permission-controlled tools.
-   A web UI usable from desktop and an Android phone browser.
-   Development work tracked in GitHub Issues/Projects that reflects
    real state.
-   Automated tests for core routing, memory, and permission behavior,
    run by Sentinel (GitHub Actions) on every push.

### Measurable targets (owner to confirm values)

  -----------------------------------------------------------------------
  Target                              Starting value
  ----------------------------------- -----------------------------------
  Monthly cloud API cost ceiling      $5 USD per month. Set by the
                                      owner as a spend limit in the
                                      Anthropic console (2026-11-14).
                                      The console limit remains the
                                      hard ceiling; AETHER does not
                                      enforce it. Code-side safeguard:
                                      the cloud output cap
                                      `AETHER_CLOUD_MAX_OUTPUT_TOKENS`
                                      (default 4096, PR #79)

  Local model VRAM budget             Leave at least 1.5 GB of the 8 GB
                                      free for the desktop; validate on
                                      the real system

  Desktop responsiveness              No visible stutter in normal
                                      desktop use while one local model
                                      answers a request

  Request round trip (cloud, short    Under 10 seconds typical
  answer)                             
  -----------------------------------------------------------------------

## 9. Constraints and assumptions

-   Existing hardware is the initial compute baseline.
-   One GPU with 8 GB VRAM is the only GPU in the initial plan.
-   Gaming and normal desktop responsiveness take priority over
    maximizing local inference throughput.
-   Cloud services may introduce API costs, rate limits, data-handling
    terms, or service outages.
-   Free tiers are not assumed to provide guaranteed production
    capacity.
-   Any paid API or hosting account and billing must be handled by an
    authorized adult account holder with permission.

## 10. Definition of done

A feature is not complete merely because an AI says it is complete.
Completion requires: - Implemented behavior. - Relevant automated tests
passing. - Documented limitations. - Security and permission review
appropriate to its risk. - User-visible status and error reporting. -
Updated documentation.
