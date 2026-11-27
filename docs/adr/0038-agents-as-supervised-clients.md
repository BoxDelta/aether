# ADR-038: Agents as supervised clients of Core

**Status:** Accepted\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

The owner will run one agent soon (the dispatcher, ADR-030) and maybe
more later, and wants Aether to see and manage them "like a parent".
ADR-009 keeps v1 small, ADR-005 (Accepted: a thin AETHER-owned
orchestrator, no agent framework in v1) applies, and
docs/04 prioritization rule 6 says not to build subsystems as separate
agents by default.

## Options

1.  No agent support in v1. Agents stay invisible to Aether.
2.  Aether hosts and runs agents itself (spawns processes, owns their
    loops). Rejected for v1: it would conflict with ADR-005, and needs
    isolation work and a larger Automata.
3.  Agents run on their own and connect to Core as supervised clients
    (chosen).

## Decision

Option 3. An agent is a client of Core, like n8n.

-   **Registry:** an `agents` table in Postgres (ADR-011): name, owner,
    purpose, status (active, paused, revoked), allowed scopes, spending
    cap, last seen.
-   **Identity:** each agent gets its own service token (ADR-035). No
    shared tokens. Only the hash is stored.
-   **Actions:** everything an agent does in Aether goes through Core's
    API and the tool permission checks (ADR-036). An agent cannot raise
    its own level or grant itself scopes. Its High actions land in the
    owner's Approvals inbox.
-   **Visibility:** every call writes events with the agent as actor
    (docs/01 §7). The Activity view can filter by agent. A separate
    Agents tab in the UI shows each agent's state, last seen, spend,
    permissions and recent actions.
-   **Control:** from the Agents tab the owner can pause (calls refused,
    nothing deleted), resume, rotate the token, revoke (token invalid at
    once) and change the cap.
    Pause and revoke are checked on every call.
-   **Memory:** agents get no Memory access by default. Any access is an
    explicit scope and follows the privacy levels in docs/05 §7.1.
-   **Heartbeat:** agents report in on a schedule; a missed heartbeat
    shows as "not seen" (not as "failed" unless an error was recorded).

Phase 3 builds this as a small step after n8n integration, reusing
tokens, tasks, events and approvals. The dispatcher is the first agent
to register.

## Consequences

-   Aether can watch, limit and stop agents, but cannot stop an agent's
    own process; revoking access is the off switch.
-   ADR-030's fold-in of the dispatcher into Nexus may later turn it from
    an agent into part of Core; revisit then.

## Revisit when

-   ADR-005 is revisited, or an agent needs Aether to start and run it.
