# ADR-011: One Postgres database for memory, tasks and events

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

Memory, runtime tasks, events and future vector search each had separate open technology questions, and Langfuse/Pulse/Neural overlapped as event consumers.

## Decision

Use one PostgreSQL instance (Docker) for memory records, runtime tasks and an append-only events table. Enable pgvector inside it only when retrieval tests show a need. No message broker in v1.

## Consequences

One thing to back up, run and understand. Possible scaling limits far beyond single-user needs.

## Revisit when

Measured performance problems, or a need for real-time streaming beyond polling/listen-notify.
