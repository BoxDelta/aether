# AETHER Phase 3 Build Order and Design Plan

**Status:** Draft (2026-11-27); ADRs 034 to 039 accepted by the owner\
**Phase:** 3 (Web UI, tools and daily use), see `04_Development_Roadmap.md`\
**Goal:** Make AETHER usable every day from the PC and the phone.

## 1. What Phase 3 must deliver (from docs/04)

- Responsive web UI: chat, memory review, Activity view, Status view.
- Tailscale access from the Android phone.
- Small tool set with permission levels from `05`, approvals in the UI.
- n8n integration for scheduled jobs and simple workflows, calling Core's
  authenticated API.
- Cancel and inspect running work.

Exit criteria: used daily from desktop and phone; tools cannot bypass
permission enforcement (tested); Activity view shows only recorded events.

**About "the app":** ADR-016 (Accepted) makes mobile web over Tailscale
the Android route; a native Kotlin app is a Future item. Phase 3 still
gives an app feel by making the web UI an installable PWA (Progressive
Web App: a website that installs to the home screen and opens full
screen, no app store). A native app needs a new ADR and is out of scope.

## 2. Decisions (accepted 2026-11-27)

| ADR | Decision | Choice |
|-----|----------|----------------|
| 034 | Web UI stack | Vite + React + TypeScript in `web/`, built to static files that FastAPI serves. No Node process at runtime. Fits the Future Three.js graph. Cost: an npm dependency tree, so Sentinel gains web lint, type check, tests and `npm audit`. Simpler option: FastAPI templates + HTMX (all Python, fewer deps, harder to grow). |
| 035 | Authentication | Two layers. (1) Network: Core listens on localhost only; `tailscale serve` gives HTTPS on the tailnet; nothing open to the internet. (2) App: single-user login (password hashed with argon2, or a passkey) giving an HttpOnly session cookie, plus CSRF protection. n8n gets its own scoped service token, never the login. |
| 036 | Tool permissions | Every tool declares a risk level from `05` §4. Low runs under granted permission; Moderate is scoped and logged; High needs approval in the UI each time; Critical is not offered in v1. Enforcement lives in `tools/`, outside the model. Answers the open question in `06` §6. |
| 037 | Live updates | Server-Sent Events (SSE: a one-way stream from server to browser) for chat streaming, task status and Activity. WebSockets only if two-way streaming turns out to be needed. |

Also decide: whether the dispatcher fold-in into Nexus (ADR-030) happens
before Phase 3 starts or runs alongside it. Recommendation: after the
3 Dec dispatcher review, as its own small track, so it does not block
Phase 3.

## 3. Build order

Each step is one or more GitHub issues, one branch and PR per issue.
Steps marked (docs/05) touch security areas and go to Claude Code with
the security-reviewer subagent, not the local path.

### Step 0: Close Phase 2
1. Phase 2 exit check (the three exit criteria in `04`), with test output.
2. Report-only code-health review; tidy the findings. Fix the open
   Phase 1 code-health findings first, before the UI builds on them:
   #36 (`create_app()` reconfigures global logging), #39 (log field
   names, one record per provider failure) and #40 (smaller findings
   backlog).
3. Merge ADRs 034 to 037.
4. Sentinel track part A (section 3a).
5. Build the llama-swap adapter (ADR-040, Accepted) before Step 4.

### Step 1: API foundation (docs/05)
1. Login, session cookie, logout, CSRF; service tokens with scopes for n8n.
2. Every existing Core endpoint requires auth (tested: no token means 401).
3. Chat endpoint that streams the reply over SSE.
4. Strict security headers: a Content-Security-Policy with no inline
   script and `frame-ancestors 'none'`. No external CDNs: fonts and
   icons are bundled with the app. Core stays bound to localhost.
5. Hardening, required before phone access (Step 5): #4 (no `server`
   header, no query strings in access logs, log destination and
   rotation) and #11 (limit on failed token attempts).

### Step 2: Runtime tasks
1. `tasks` table in the same Postgres (ADR-011) with the statuses in
   `01` §6, linked to request IDs and events.
2. Timeouts on every task; cancel endpoint; inspect endpoint
   (status, events, outcome).
3. Status changes driven by execution events only (tested).

### Step 3: Web skeleton
1. `web/` app, built by Sentinel, served by FastAPI at `/`.
2. Sentinel jobs for `web/`: lint, type check, unit tests, `npm audit`.
3. Login screen, layout and navigation (see section 4).

### Step 4: Chat view
1. Streaming chat, session list, cancel button.
2. "via Claude" / "via local" label in the UI, not in the reply text
   (open question in `07` §6).
3. Memory save proposals from chat appear as approve/reject cards.

### Step 4b: Topics (ADR-039)
1. `chats` table (one main chat, many topics) and `chat_id` on
   messages.
2. Aether creates topics; "New topic" link above the reply with
   "Use an existing one" (merge).
3. Routing of main-chat messages on the local model; "Added to" link and
   Move; unsure means it stays in the main chat (tested).
4. Topics sub-tab under Chat; archive after 14 quiet days with a Memory summary
   proposal; reopen and merge.

### Step 5: Phone access (start daily use here)
1. `tailscale serve` set up and documented in `docs/`.
2. PWA manifest, icons and service worker (caches the app shell only,
   never memory or chat content).
3. Install on the Android phone; check every view at phone width.

### Step 6: Memory review view
1. List and search memory with privacy level and provenance shown.
2. Correct, delete, export, using the Phase 2 APIs.
3. Pending proposals queue.

### Step 6b: Read APIs for Activity and Status
1. `GET /events`: filters (type, request, task), paging, metadata only,
   the same columns as the table. Tested: it returns exactly the
   recorded rows.
2. `GET /status`: health, readiness, provider availability, the GPU/RAM
   probe.
3. Both are built and tested before their views (Steps 7 and 8).

### Step 7: Activity view
1. Reads the `events` table only; filters by type, request, task.
2. Event detail with safe metadata; no payloads with secrets.
3. Test: the view never shows an event that is not in the table.

### Step 8: Status view (Pulse basics)
1. Service health, provider availability, last errors.
2. GPU/RAM from the existing probe; estimated cloud spend against the
   monthly limit.
3. Token counts (today and this month, cloud vs local) from the usage
   metadata providers return; today's total also in the sidebar status
   card.

### Step 9: Tools framework (docs/05)
1. `tools/` registry: typed inputs and outputs, declared risk level,
   timeout, audit event per call.
2. Approval flow: `approval.requested` / `granted` / `denied` events;
   High tools pause until approved in the UI.
3. Bypass tests: a model reply, memory text or tool output that asks for
   a tool or a higher level is refused.
4. First tools, kept to three or four: read GitHub issue status
   (Low, read-only token), create a runtime task or reminder (Moderate),
   save a note to Memory through the normal approval flow (Moderate).
   Web fetch or file write only after these are proven.

### Step 10: Approvals in the UI
1. Approvals inbox with badge on every view; approve/deny with reason.
2. Approvals expire after a set time and are re-checked at execution.

### Step 11: n8n integration (docs/05)
1. n8n calls Core with its scoped service token over the Docker network.
2. One real scheduled job first (for example a daily summary or a
   backup check), with permissions re-checked when it runs.
3. Its runs show up as tasks and events like any other request.

### Step 11b: Agent registry (ADR-038, docs/05)
1. `agents` table, per-agent service token, scopes and spending cap.
2. Pause, resume and revoke, checked on every call (tested).
3. Agents tab (state, last seen, spend, permissions, recent actions,
   pause/rotate/revoke); agent filter in Activity.
4. Register the dispatcher as the first agent.

### Step 12: Phase 3 exit
1. Docs update: docs/01, 02, 05, 06, CODE_TOUR, QUICK_REF and README
   match what was built.
2. Exit-check runbook at `docs/runbooks/phase3-exit-check.md`.
3. One to two weeks of daily use from PC and phone.
4. Exit criteria checked with the runbook, with test output.
5. Code-health review and tidy.

## 3a. Sentinel track (deterministic checks only)

Prioritization rule 4 in `04`: deterministic checks come before any AI
reviewer. The AI review layer stays a Future item.

| Part | Lands with | Checks |
|------|-----------|--------|
| A | Step 0 | Dependency audit (`pip-audit`); Dependabot alerts; static security scan (CodeQL, free on public repos); a "new dependency" check that flags any change to `pyproject.toml`, lockfiles or `web/package.json` in the PR summary |
| B | Step 3 | `web/` jobs: ESLint, `tsc` type check, Vitest unit tests, production build, `npm audit` (fail on high); licence check for new packages (compatible with Apache-2.0) |
| C | Step 5 | Playwright smoke tests (a scripted browser): login, send a chat message, cancel, at desktop and phone width |
| D | Step 9 | Tool bypass test suite marked as a required check |

## 3b. Who builds each step

"Local" means the dispatcher's local path (ADR-030), "Claude" means
Claude Code. A step marked Split becomes two issues: one labelled
`dispatch:local-ok` and one labelled `dispatch:claude-only`. A step marked
Claude-only is one issue labelled `dispatch:claude-only`.

| Step | Who builds it |
|------|---------------|
| 0 | Split: identity fix #195 and small code-health fixes → local. Adapter, Sentinel jobs → Claude |
| 1 API foundation | Claude-only |
| 2 Runtime tasks | Split: tests → local. State machine, cancel, timeouts → Claude |
| 3 Web skeleton | Split: Vite scaffold, layout, sidebar → local. Login screen, Sentinel web/ jobs → Claude |
| 4 Chat view | Split: message list, composer UI → local. SSE streaming → Claude |
| 4b Topics | Claude-only |
| 5 Phone access | Split: PWA manifest, icons, docs → local. Service worker caching rules → Claude |
| 6 Memory view | Split: list, filters, detail UI → local. Wiring delete/export → Claude |
| 7 Activity view | Split: GET /events → Claude. View → local |
| 8 Status view | Split: GET /status, token counts → Claude. Cards → local |
| 9 Tools framework | Claude-only |
| 10 Approvals UI | Split: inbox UI → local. Approval logic → Claude |
| 11 n8n integration | Claude-only |
| 11b Agent registry | Claude-only |
| 12 Exit | Split: runbook docs → local. Exit check → Claude |

## 4. UI design plan

- **Layout:** sidebar on desktop, bottom tab bar on the phone. Desktop
  tabs: Chat (the main chat, with Topics as a sub-tab shown only
  while Chat or Topics is open), Memory, Activity, Status, Agents,
  Approvals (badge when something waits). The phone keeps five tabs;
  topics open from links in the main chat (ADR-039).
- **Look:** dark theme by default, one accent colour, readable mono font
  for events and IDs. Branded names (Neural, Pulse) in the UI, plain
  names in code (ADR-012).
- **Honesty rules in the UI:** show only recorded events; show failed
  and cancelled work plainly; never show model reasoning as activity
  (ADR-007).
- **Phone first:** every screen usable one-handed at 390 px wide; no
  hover-only controls.
- **No offline copies** of memory or chat on the phone (Connect is not a
  source of truth, `01` §3 rule 8).
- **Design before code:** a Design System (colours, type, spacing,
  components) and mockups of the five screens are made and approved
  before Step 3. Claude Code builds from them with Tailwind + shadcn/ui
  and the frontend-design skill, so it does not invent styles per screen.
- **Component sources:** shadcn/ui is the base. Extra blocks may come
  from MIT-licensed shadcn-style registries: BagUI (dashboard metrics,
  tables, activity) for Activity and Status, and Spell UI for one or two
  accent effects (login, empty states). TripleD UI only once its licence
  is confirmed. Every pulled component is reviewed like our own code;
  Next.js-only parts are adapted for Vite; Motion is the only animation
  library; animations respect "reduce motion" and stay light on the phone.

## 5. Out of scope for Phase 3

Native Android app, Neural 3D graph, Oracle, Vault service, AI review
layer, hosted gateway, voice, extra provider adapters beyond what ADRs
already accept. Raise a new ADR if daily use shows one is needed.
