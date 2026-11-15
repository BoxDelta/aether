# ADR-014: Merge Pulse into a Neural Status view

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

Pulse, Neural and Langfuse would all read the same events.

## Decision

Pulse is a Status view inside the web UI, fed by the events table and a small GPU/CPU/RAM probe that also informs Nexus. Langfuse is optional later.

## Consequences

One UI to build. Less advanced metrics at first.

## Revisit when

Monitoring needs outgrow a simple view.
