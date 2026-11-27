# ADR-034: Web UI stack

**Status:** Accepted\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

Phase 3 (docs/04) needs a responsive web UI: chat, memory review,
Activity, Status and Approvals, usable on the Android phone over
Tailscale (ADR-016). docs/03 lists "React (or simpler)" as a Candidate.
The Future Neural 3D graph will use Three.js.

## Options

1.  FastAPI templates + HTMX. All Python, few dependencies; harder to grow
    into live, app-like screens and the 3D graph.
2.  Vite + React + TypeScript, built to static files served by FastAPI
    (chosen).
3.  Next.js or another server framework. Rejected: adds a Node server at
    runtime next to Core.

## Decision

Option 2.

-   Code lives in `web/`. Vite builds it to static files; FastAPI serves
    them at `/`. No Node process runs at runtime.
-   Styling: Tailwind CSS. Components: shadcn/ui (component code is
    copied into `web/src/components/ui/`, so it is reviewed like our own
    code). Icons: lucide-react.
-   API types for the UI are generated from FastAPI's OpenAPI schema, not
    written by hand.
-   The UI is a PWA (installable web app). Its service worker caches the
    app shell only, never memory, chat or event data (docs/01 §3 rule 8).
-   A lockfile is committed. New npm packages follow the docs/03 §8
    checklist like Python ones.

## Consequences

-   An npm dependency tree enters the repo. Sentinel gains jobs for
    `web/`: lint, type check, unit tests, build and dependency audit.
-   Claude Code needs Node only at build time on the PC and in CI.

## Revisit when

-   The UI stays simple enough that HTMX would have done, or
-   a native Android app is approved (would need a new ADR; ADR-016).
