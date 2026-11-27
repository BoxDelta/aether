# ADR-037: Live updates with Server-Sent Events

**Status:** Accepted\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

The UI needs streamed chat replies, live task status, new events in the
Activity view and approval badges.

## Options

1.  Polling. Simple but slow and wasteful.
2.  Server-Sent Events (SSE): a one-way stream from server to browser
    over normal HTTP (chosen).
3.  WebSockets: two-way. More moving parts; not needed when the browser
    sends actions as normal requests.

## Decision

Option 2. Streams use the same session auth as other endpoints. Each
stream sends only data the session may see, and never secrets. Clients
reconnect with the last event ID so nothing is missed. Actions (send
message, cancel, approve) stay as normal authenticated POST requests.

## Revisit when

-   A feature truly needs two-way streaming (for example live voice,
    a Future item).
