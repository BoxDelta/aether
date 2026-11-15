# ADR-013: Use existing n8n for scheduling and simple workflows

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

n8n already runs in Docker on the host PC. Building Chronos and simple Automata workflows from scratch duplicates it.

## Decision

In v1, scheduled jobs and simple multi-step workflows run in n8n and call AETHER Core through its authenticated API. Permission checks still happen in Core.

## Consequences

Less code to write. n8n becomes a dependency; its credentials must follow Vault rules.

## Revisit when

n8n cannot meet audit, permission or reliability needs.
