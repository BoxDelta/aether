# ADR-036: Tool permission levels for v1

**Status:** Accepted\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

Phase 3 adds a small tool set (Automata, `tools/`). docs/05 §4 gives four
risk levels but leaves the exact rules per tool open, and docs/06 §6 asks
which actions may run without confirmation.

## Decision

-   Every tool declares, in code: typed inputs and outputs, a risk level,
    a timeout, and the scopes it needs.
-   **Low** (read-only, approved sources): runs under granted permission;
    logged.
-   **Moderate** (scoped writes inside AETHER, such as creating a runtime
    task): runs under a scoped permission; logged with inputs summarised.
-   **High** (deletes, sending or publishing outside AETHER, changes
    outside the workspace): pauses and asks for approval in the UI every
    time. Approvals expire and are re-checked when the tool runs.
-   **Critical** (security controls, credentials, deploys): not offered as
    a tool in v1.
-   Enforcement lives in `tools/` and runs before every call. A model
    reply, memory text, tool output or web content can never raise a level
    or grant a permission (docs/05 §2, §6).
-   Each call writes `tool.execution.*` and, where needed, `approval.*`
    events (docs/01 §7).
-   First tools are limited to three or four Low and Moderate tools.

## Consequences

-   Bypass tests are required in Sentinel before any tool merges.
-   Adding a High tool needs its own issue with a security review.

## Revisit when

-   A tool needs a level that does not fit, or approvals become so
    frequent that daily use suffers.
