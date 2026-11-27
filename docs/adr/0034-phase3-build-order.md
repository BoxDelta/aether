# ADR-034: Phase 3 design and build order

**Status:** Proposed\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

Phase 2 is done (exit check passed 2026-11-27). Phase 3 makes AETHER
usable every day from the PC and the phone (docs/04):

-   a responsive web UI: chat, memory review, Activity view, Status view;
-   Tailscale access from the Android phone (ADR-016);
-   a small tool set with the permission levels in docs/05 §4, approvals
    in the UI;
-   n8n for scheduled jobs and simple workflows, calling Core's
    authenticated API (ADR-013);
-   cancel and inspect running work.

Exit criteria: used daily from desktop and phone; tools cannot bypass
permission enforcement (tested); the Activity view shows only recorded
events.

What exists and constrains the design:

-   Core is FastAPI (ADR-017), bound to `127.0.0.1`, with one static
    bearer token on every route except `/health` (ADR-021, #57). ADR-021
    says to revisit browser access in Phase 3.
-   `/chat` answers in one JSON response; there is no streaming and no
    record of work in progress, so nothing can be cancelled.
-   `/memory` already has list, search, create, approve, reject,
    correct, delete and export. Models can propose memory (ADR-033);
    proposals wait as `pending`.
-   `events` holds metadata only (docs/05 §5, §9). There is no read API
    for it yet.
-   Open hardening deferred to "before remote access": #4 (server
    header, sessions, query strings in access logs, log rotation) and
    #11 (no rate limit on failed tokens).
-   ADR-029's end-of-phase code-health review was not run for Phase 2,
    and the Phase 1 findings #36, #39 and #40 are still open although
    ADR-029 said to handle them before Phase 2 exits.

## Decisions for the owner

Each has a recommended default, the main alternative and the trade-off.

-   **D1. Web UI stack.** Recommended: pages rendered by Core (Jinja2
    templates) with htmx and a small hand-written CSS file, served from
    Core's own origin. No Node toolchain and no npm dependencies.
    Alternative: a TypeScript single-page app (for example Svelte) built
    with Vite and served as static files. Trade-off: server-rendered is
    one language, one process and no JavaScript supply chain, and fits
    the views Phase 3 needs (forms, lists, a chat log); a SPA gives
    richer interaction (live updates, offline drafts) at the cost of a
    second toolchain, a build step in Sentinel and Dependabot for npm.
-   **D2. Browser sign-in** (revisits ADR-021). Recommended: one owner
    password (stored as an Argon2 hash in `.env`), then an HttpOnly,
    Secure, `SameSite=Strict` session cookie with a server-side session
    row in Postgres, an idle and an absolute expiry, a CSRF token on
    every form, and a failed-login limit (covers #11 for browsers). The
    bearer token stays for n8n and scripts. Alternative: trust
    Tailscale identity headers from `tailscale serve` (ADR-021 option
    3). Trade-off: a password works the same on the PC (loopback) and
    the phone and doesn't tie AETHER to Tailscale; Tailscale identity
    needs no password but leaves the PC's own browser needing another
    path. A passkey can be added later.
-   **D3. How the phone reaches Core.** Recommended: `tailscale serve`
    on the PC gives HTTPS on the tailnet name and proxies to Core on
    `127.0.0.1`; Core keeps its loopback bind. Alternative: bind Core
    to the Tailscale interface address. Trade-off: `serve` gives a real
    certificate (needed for Secure cookies) and keeps Core's bind
    unchanged; binding directly needs TLS in Core or plain HTTP.
-   **D4. The first tools.** Recommended: start with two Low tools and
    one High tool so both paths are exercised:
    -   Low: read the status of AETHER's own GitHub issues (read-only
        token scope); read the time and date in the owner's zone.
    -   High (needs approval in the UI): create a GitHub issue in
        AETHER's repository.
    Alternative: pick from daily use first and build the framework with
    fake tools only. Trade-off: real tools prove the approval path end
    to end; fake-only delays that proof. Each tool's level is fixed in
    code, never chosen by the model (docs/05 §4).
-   **D5. Cancel and inspect.** Recommended: Core keeps an in-memory
    registry of running `/chat` work (request ID, start time, provider,
    state); `GET /work` lists it and `POST /work/{id}/cancel` cancels
    the task, recorded as an event. Nothing is persisted: a restart
    ends all work. Alternative: a persisted job table with workers.
    Trade-off: in-memory is enough for one user and one process;
    persisted jobs are needed only for long work, which v1 doesn't have.
-   **D6. Streaming answers.** Recommended: not in Phase 3's first
    pass; the chat view shows a "working" state with a Cancel button.
    Alternative: stream tokens (SSE) now. Trade-off: streaming needs
    the echo and redaction checks to work on partial text, which is a
    design of its own; add it later if daily use shows the wait hurts.
-   **D7. n8n scope.** Recommended: one scheduled workflow first (for
    example a morning summary sent to Telegram, under the existing
    Telegram rules in CLAUDE.md), calling Core with the bearer token,
    plus a runbook. Alternative: several workflows. Trade-off: one
    proves the path; more can follow daily use.

## Decision

Pending the owner's answers to D1–D7. The build order below assumes
the recommended defaults.

## Build order (small PRs)

In order. Each is one issue, one branch, one PR. The build order is
tracked in one issue ("Phase 3: build order (ADR-034)"); per-step issues
are created as each step starts. Existing issues are named where they
fit.

0.  **Tidy first.** Run the Phase 2 code-health review (ADR-029) and
    file its findings; fix #36, #39 and the open #40 items that the UI
    builds on.
1.  **Hardening before remote access:** #4 (no `server` header, query
    strings out of access logs, log destination and rotation) and #11
    (failed-token limit).
2.  **Read APIs the views need:** `GET /events` (filters, paging,
    metadata only, the same columns as the table) and `GET /status`
    (health, readiness, the GPU/RAM probe). Tests that the Activity
    data is exactly the recorded rows.
3.  **Browser sign-in (D2):** new ADR amending ADR-021, sessions
    table (migration), login and logout, CSRF, security headers (CSP
    with no inline script, `frame-ancestors 'none'`), the failed-login
    limit. Security review.
4.  **UI skeleton (D1):** layout, navigation, login page, static
    files served by Core, no external CDNs.
5.  **Chat view:** a conversation in the browser over `/chat`, the
    routing reason and provider shown, the `allow_cloud` and
    `include_sensitive` switches off by default per request.
6.  **Memory review view:** pending proposals (approve with a level,
    reject), search, correct, delete, export.
7.  **Activity view:** events from step 2, read-only.
8.  **Status view (Pulse basics, ADR-014):** health, readiness, GPU/RAM.
9.  **Phone access (D3):** `tailscale serve` runbook, the phone tested
    on every view, responsive fixes.
10. **Cancel and inspect (D5):** the work registry, `GET /work`,
    cancel, events, and the controls in the chat view.
11. **Tool framework:** tool contract in `router/` (provider-neutral),
    per-tool fixed level, Core-side enforcement, every call and
    decision recorded as events, timeouts. Tests that a model cannot
    run a tool above its granted level or skip an approval (the exit
    criterion).
12. **Approvals in the UI:** pending tool calls shown, approve or deny,
    expiry.
13. **First tools (D4).**
14. **n8n (D7):** the workflow, its runbook, and Core's bind for the
    `n8n-net` gateway if needed (ADR-021).
15. **Docs:** docs/01, 02, 05, 06, CODE_TOUR, README, QUICK_REF.
16. **Exit check runbook** (`docs/runbooks/phase3-exit-check.md`) and
    a period of daily use from both devices.

## Consequences

-   Easier: security work (hardening, sign-in) lands before anything is
    reachable from the phone; every view sits on a tested read API.
-   Harder: no streaming and no persisted jobs at first; both may be
    asked for in daily use.
-   Risks: a browser UI is a new attack surface (XSS, CSRF, session
    theft). Steps 1 and 3 and the security review on each UI PR are the
    controls.

## Revisit when

The owner answers D1–D7 (then this ADR becomes Accepted with the
answers recorded), or daily use shows streaming, persisted jobs or a
native app are needed.
