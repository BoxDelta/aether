# ADR-012: Plain code names, branded doc names

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

Branded names (Nexus, Automata, Neural) are clear in docs but make code harder to navigate for people and AI assistants.

## Decision

Code folders use plain names: core/, router/, memory/, tools/, web/, identity/. Docs and UI keep AETHER names. 02_Subsystem_Reference.md maps between them.

## Consequences

Easier navigation; one mapping table to maintain.

## Revisit when

Never, unless the mapping causes confusion.
