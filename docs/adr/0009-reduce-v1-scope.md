# ADR-009: Reduce v1 scope to Core, Nexus, Memory and web UI

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

The original plan had 11 subsystems across 8 phases, sized for a team. AETHER is built by one person; a plan that large risks never reaching a usable state.

## Decision

v1 contains Core, Nexus (Claude + Ollama), Memory and a responsive web UI, plus Sentinel checks in CI. Neural 3D graph, native Chronos, Oracle, native Android app, Sentinel AI review, Vault service and hosted gateway move to a Future list. Roadmap reduced to Phases 0 to 3.

## Consequences

AETHER becomes useful sooner and each part is tested in real daily use before more is built. Future items are deferred, not removed.

## Revisit when

v1 is in daily use, or a Future item becomes necessary for v1 to be useful.
