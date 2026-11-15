# ADR-005: Thin AETHER-owned orchestrator

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

Core needs an orchestration layer: resolve identity and session, ask
Nexus for a route, call the provider, record events. ADR-005 was first
recorded as "Evaluate OpenClaw as initial orchestration foundation"
(Proposed). v1 scope is one request path with two providers (ADR-009).

## Decision

Build a thin AETHER-owned orchestrator in `core/`, using plain Python
and FastAPI (ADR-017). Do not adopt OpenClaw or another agent framework
for v1.

Options considered:

- **Thin AETHER-owned orchestrator (chosen):** small, fully tested,
  easy to reason about for security and permission enforcement; no
  third-party agent runtime between Core and the providers.
- **OpenClaw:** more built-in capability, but a larger dependency and
  security surface to validate before v1 needs it.

## Consequences

AETHER owns more code (routing, failover, tool calls later), and must
keep it small. Security review is simpler. Multi-step agent behaviour
is out of scope until a need is shown in daily use.

## Revisit when

v1 is in daily use; re-evaluate OpenClaw (or another framework) if the
orchestrator grows complex or a needed capability is costly to build.
