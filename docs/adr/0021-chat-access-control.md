# ADR-021: How clients authenticate to Core (/chat)

**Status:** Superseded by ADR-035 (was Accepted: option 2, bearer token; implemented in #57)\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

`POST /chat` (#22) can spend cloud API money (`allow_cloud=true`), and
later it will read memory. docs/01 §5 says clients reach Core "over an
authenticated channel", and §9 says "authenticated local connectivity for
clients; Tailscale for the phone". docs/05 doesn't say how. No ADR
decided the mechanism.

Callers expected in v1: the web UI in a browser on the PC and on the
Android phone over Tailscale (ADR-016), n8n calling Core's API (ADR-013),
and `curl`/scripts on the PC.

Core binds to `127.0.0.1` by default (uvicorn's default; README command).

**n8n runs in Docker on this PC**, so it reaches Core from a
non-loopback address. Checked 2026-11-14:

-   The container `n8n` is on the bridge network `n8n-net` (a private
    Docker subnet, with its own container and gateway addresses), with
    `host.docker.internal:host-gateway`.
-   With Core on `127.0.0.1`, n8n can't connect at all.
-   With Core bound where n8n can reach it, the connection arrives from
    the Docker network gateway address, which isn't loopback, so the interim check refuses it.

n8n is therefore blocked today, and only the token (or a deliberate
exception) can let it in.

**Interim, in #22:** `/chat` refuses any client whose address isn't
loopback (403). That matches the default bind and needs no secret.

**How uvicorn's proxy headers affect that check** (uvicorn 0.54, tested in
`tests/test_chat.py`): uvicorn runs with `proxy_headers=True` by default
and trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`, which
defaults to `127.0.0.1,::1`. When the direct client is trusted, uvicorn
replaces `request.client.host` with the right-most untrusted address in
`X-Forwarded-For`. So:

-   A remote client sending `X-Forwarded-For: 127.0.0.1` directly isn't
    trusted. Its header is ignored and it's refused.
-   A local reverse proxy that sets `X-Forwarded-For` (appending the real
    client) makes the check see the real remote address, so the request
    is refused. A spoofed `127.0.0.1` earlier in the header doesn't help,
    because the right-most untrusted entry wins.
-   **The check is bypassed** by a local proxy that doesn't send
    `X-Forwarded-For`, by running uvicorn with `--no-proxy-headers`, or
    by widening `FORWARDED_ALLOW_IPS` (for example `*`). In those cases a
    proxied remote client looks like `127.0.0.1`.

Until the token exists: no reverse proxy in front of Core, no
`--no-proxy-headers`, no wider `FORWARDED_ALLOW_IPS`, and no bind other
than loopback.

## Options

1.  **Loopback only (keep the interim).** No secret to manage. It blocks
    the phone and n8n (see above), so it doesn't meet ADR-016 or ADR-013.
2.  **Static bearer token.** `AETHER_API_TOKEN` in `.env` (name in
    `.env.example`, value never committed), sent as
    `Authorization: Bearer …`, compared in constant time, and added to
    the log redactor. Simple, and works for curl, n8n and the phone.
    One shared secret means no per-device revocation, and the browser
    UI needs somewhere to keep it.
3.  **Tailscale identity.** Core is reached only through
    `tailscale serve`, which adds `Tailscale-User-Login` headers. Core
    trusts those headers only from the loopback proxy, and only for
    allowed logins. There's no secret in AETHER, but it ties
    authentication to Tailscale, and local curl/n8n need a separate path.
4.  **Login session for the web UI.** Password or passkey, then an
    HttpOnly session cookie. Needed for a browser UI eventually, and the
    most work. On its own it doesn't serve n8n.

## Decision

Option 2: a bearer token on every route except `/health`. Accepted by
the owner on 2026-11-14. Implementation is #57. **The interim
loopback-only check stays until the token is implemented**. #57 then
decides whether the loopback check is removed or kept as defense in
depth. Revisit for Phase 3 (web UI), likely by adding option 4 or 3 for
browsers and keeping the token for n8n and scripts.

## Implementation (#57)

-   `core/auth.py`, ASGI middleware in front of every route. Only
    `GET /health` is public. `/docs`, `/redoc`, `/openapi.json`, unknown
    paths and any route added later all need the token. WebSocket
    connections are refused until one needs authenticating.
-   **No token configured, or one under 32 characters or padded with
    spaces:** Core starts, logs one warning, serves `/health`, and answers
    every other route with 503 `AuthNotConfigured`. It never runs open.
-   Missing or wrong token: 401 `Unauthorized` with
    `WWW-Authenticate: Bearer`. The body has no header value, and the log
    has one warning with a reason category only (missing / malformed /
    invalid).
-   Auth runs before the body is read, so unauthenticated clients can't
    make Core buffer a request body.
-   **The interim loopback check is removed.** The token is now the access
    control. Keeping the loopback check would keep n8n (Docker, via
    the network gateway address) and the phone blocked, which is what this ADR was for.
    The proxy-header caveats above stop mattering for access, because a
    proxied request still needs the token. What a client can reach is
    still limited by the bind address, which defaults to `127.0.0.1`.
    Binding elsewhere (for n8n, the `n8n-net` gateway) is the owner's
    choice, and is safe only with the token set.

## Consequences

-   A new secret in `.env`, redacted in logs, rotated by editing `.env`
    and restarting Core.
-   Tests must prove every route except `/health` refuses requests
    without the token, that the comparison is constant-time, and that
    the token never appears in logs, events or error bodies
    (acceptance criteria in #57).
-   For n8n to reach Core, Core must listen on an address the container
    can reach (for example the `n8n-net` gateway). That is only safe once
    the token is required.

## Revisit when

The web UI (Phase 3) lands, more than one person uses AETHER, or Core is
reached from outside Tailscale.
