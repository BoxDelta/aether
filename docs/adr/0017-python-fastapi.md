# ADR-017: Python + FastAPI backend

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

A backend language is needed for Phase 1. The backend mostly calls model
providers (Claude API, Ollama), reads and writes Postgres, and serves an
HTTP API for the web UI and n8n.

## Decision

Use Python 3 with FastAPI.

Options considered:

- **Python + FastAPI (chosen):** first-class SDKs for Anthropic and
  Ollama; strongest ecosystem for embeddings and pgvector; async suits
  I/O-bound model calls; easiest for AI coding assistants to write and
  test; mature Postgres tooling (SQLAlchemy, Alembic).
- **TypeScript/Node:** good for web-first projects and could share a
  language with the UI, but the AI and retrieval ecosystem is weaker.

## Consequences

The web UI may use JavaScript/TypeScript and talks to the backend over
HTTP, so the two do not need to share a language. Tooling for Sentinel
in Phase 1: pytest, ruff (lint/format), mypy or pyright (types).

## Revisit when

The orchestration framework decision (ADR-005) requires another
language, or measured performance needs cannot be met.
