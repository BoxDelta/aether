# ADR-035: Web and API authentication

**Status:** Accepted\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

The web UI shows memory, events and approvals, and can start tools.
n8n (ADR-013) and later agents must call Core too. docs/05 requires
least privilege and no secrets in prompts, logs or Git.

## Options

1.  Tailscale only, no app login. Rejected: any device on the tailnet
    would have full access.
2.  App login only, Core reachable on the LAN. Rejected: larger exposure.
3.  Two layers: Tailscale network access plus an app login, and scoped
    service tokens for machines (chosen).

## Decision

Option 3.

-   **Network:** Core binds to `127.0.0.1`. `tailscale serve` provides
    HTTPS on the tailnet. Nothing is exposed to the internet (no
    `tailscale funnel`, no port forwarding).
-   **People:** single-user login. Password hashed with argon2 (passkey
    may be added later). Success gives an HttpOnly, Secure, SameSite=Strict
    session cookie with an expiry. State-changing requests need a CSRF
    token. Failed logins are rate-limited and logged as events.
-   **Machines:** service tokens, one per client (n8n, each agent), each
    with a named list of scopes. Only a hash of the token is stored. Tokens
    can be revoked from the UI. n8n reaches Core over the Docker network,
    not through the login.
-   Every Core endpoint except health requires a session or a token
    (tested). Login secrets and token values live in `.env` only; names go
    in `.env.example`.

## Consequences

-   Adds an auth module and a `service_tokens` table to Postgres
    (ADR-011).
-   Security-sensitive: PRs touching auth follow the docs/05 review path.

## Revisit when

-   A hosted gateway is planned (Future), or a second human user is added.
