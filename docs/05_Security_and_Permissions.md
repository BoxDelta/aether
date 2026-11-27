# AETHER Security and Permissions Specification

**Status:** Initial security baseline\
**Principle:** The model can propose actions; trusted platform code
enforces permissions.

## 1. Security objectives

-   Protect user data, credentials, and project files.
-   Prevent untrusted content from becoming executable instructions.
-   Limit the impact of a compromised model, tool, or integration.
-   Make consequential actions visible and auditable.
-   Support recovery from mistakes and failures.
-   Minimize data sent to external providers.

## 2. Trust boundaries

Treat these as separate trust domains: 1. User and client devices. 2.
AETHER Core and internal services. 3. Model providers (local and cloud).
4. Tool adapters and execution environments. 5. External websites,
repositories, documents, and messages. 6. Third-party integrations and
task trackers.

Client channels into the development PC belong to domain 1. Besides the
terminal, that includes the Telegram channel into the Claude Code session
(ADR-027, Proposed): Telegram is not end-to-end encrypted, its pairing
allowlist holds only the owner, and its bot token is a credential kept
outside the repository. The dispatcher's Discord webhook (ADR-030) posts
to a channel in a private server where only the owner is a member. Until
step 4d it is outbound only: it posts dry-run results (issue number and
link, route, step, a reason of at most 200 characters, mentions off) and
reads nothing back. The reason is model text and may quote the issue.
The dry-run timer runs from a main-only clone (`~/Projects/aether-dispatch`),
so only merged, reviewed code runs with the dispatcher's credentials.
Discord is not end-to-end encrypted. The webhook URL
is a credential, kept in `~/.config/aether-dispatch/dispatch.env`.

Content returned by a model, website, repository, document, or plugin is
data, not trusted policy. It must not override AETHER's system
permissions.

## 3. Least privilege

-   Give each tool only the access it needs.
-   Scope credentials to the smallest practical permission set.
-   Separate read-only tools from write-capable tools.
-   Separate development workspaces from personal files.
-   Avoid running agent services as root.
-   Do not expose the host filesystem or shell without an explicit,
    bounded policy.
-   Re-evaluate permissions when scheduled jobs execute.

## 4. Approval levels

Suggested baseline:

  -----------------------------------------------------------------------
  Risk level              Examples                Default control
  ----------------------- ----------------------- -----------------------
  Low                     Read approved project   May run under granted
                          status; summarize a     read permission
                          selected document       

  Moderate                Create a task; edit a   Scoped permission; log
                          project file in a       action
                          workspace               

  High                    Delete data; modify     Explicit user approval
                          system settings;        
                          publish or send         
                          externally              

  Critical                Change security         Human approval and
                          controls; deploy        independent review
                          sensitive services;     
                          alter credential policy 
  -----------------------------------------------------------------------

The exact action classification must be defined per tool. A model's
confidence is not a substitute for authorization.

## 5. Credential handling

-   Store secrets outside source control.
-   Use environment variables or a dedicated secret manager for
    development.
-   Never include credentials in prompts, logs, screenshots, task
    titles, or Git history.
-   Redact tokens and sensitive values from error reports.
-   Client access to Core: bearer token (`AETHER_API_TOKEN`, at least
    32 characters) on every route except `/health` (ADR-021, #57),
    compared in constant time. Without a valid token configured, Core
    serves only `/health` and answers 503 elsewhere; it never runs open.
    The token is registered with the log redactor, failed attempts are
    logged without the header value, and it never appears in events or
    error bodies. Core still binds to `127.0.0.1` by default. Bind it to
    another interface only with the token set.
-   `/chat` error bodies carry only the error type, the routing reason
    and each attempt's provider, error type and scrubbed message. Never
    exception text, `__cause__` or a traceback. Validation errors never
    echo the submitted input, and name an unknown field only if the name
    is one of the API's own (`source` sent where it isn't allowed);
    any other name becomes `<unknown field>` (#94).
-   Logs redact configured secrets by value (`ANTHROPIC_API_KEY`,
    `AETHER_API_TOKEN`, and the `DATABASE_URL` and
    `DATABASE_MIGRATION_URL` passwords as written and percent-decoded), Anthropic-key-shaped strings, `Bearer <credential>`
    values, and the password in any `scheme://user:password@host` URL,
    configured or not (`core/log.py`; its module docstring is the full
    list). Database errors are reported from safe parts only
    (`core/db.py`, ADR-020).
-   The Claude client takes its key and endpoint from AETHER's settings:
    `router/claude.py` passes the API key and the fixed base URL
    `https://api.anthropic.com` explicitly (and `auth_token=None`), so a
    stray `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`,
    `ANTHROPIC_AUTH_TOKEN` or credentials profile in the environment
    cannot change the host or the credential source (checked against
    anthropic 2.8.0; tested). **Known limitations, tracked in #18:** the
    SDK still reads `ANTHROPIC_CUSTOM_HEADERS`, which can add or override
    any request header, including `Authorization` (the host stays fixed
    and AETHER's key does not leak, but the call could run under another
    credential); its default HTTP client honours proxy variables such as
    `HTTPS_PROXY`; and `ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE`
    could put prompt content into tracing spans if an exporter were
    configured (AETHER configures none).
-   Events hold metadata only: IDs, privacy level, route decision and
    reason, scrubbed error type/message, latency, token counts. They never
    hold prompts, answers, memory content, secrets or tracebacks. This is
    enforced by typed columns with CHECK constraints, redaction on write,
    and tests (`core/events.py`, docs/01 §7). Memory events (migration
    0005, ADR-024 §5) add only IDs, category, source, levels and counts,
    never memory content, keys, corrected text, search queries or
    rejection reasons, and are written in the same transaction as the
    memory change, so neither exists without the other. The migration
    changes no grants, and its downgrade refuses while memory events
    exist rather than delete audit rows. A memory event can't carry the
    free-text columns (route reason, error message, attempts), by CHECK.
    **Known limitation:** `actor="restore"` is a label, not proof: the
    app role can also write it. The restore repair
    (`memory/restore.py`, step 16) runs as the owner role, but nothing
    enforces that yet (#150).
-   Database roles (#1, #12, ADR-020 addition 2026-11-16): Core connects
    as `aether_app` (`DATABASE_URL`), which has only CONNECT, schema
    USAGE, INSERT and SELECT on `events`, SELECT on `alembic_version`,
    and SELECT, INSERT, UPDATE and DELETE on `memory_records` (migration
    0004, #66: Core corrects and hard-deletes memory records, ADR-024
    §4). It owns nothing, so it can't update, delete or truncate events,
    drop or disable the append-only triggers, truncate `memory_records`
    or add triggers to it, or create, alter or drop tables. The owner
    `aether` (`DATABASE_MIGRATION_URL`) runs migrations only. Nothing is
    granted to PUBLIC. Core refuses to start, and `/ready` reports
    `misconfigured`, if its role is a superuser, owns the database or any
    table or function, can create objects, holds TRUNCATE or TRIGGER on
    any table, holds UPDATE or DELETE on any table other than
    `public.memory_records`, or is the same as the migration role (tested
    live). Database passwords
    shorter than 8 characters are refused as `misconfigured`, without
    echoing them, because shorter values aren't redacted by value. The
    app role's password is set as a SCRAM verifier, never sent in plain
    text (`core/db_admin.py`). Upgrade steps for an existing install:
    `docs/runbooks/db-app-role.md`.
-   **Known limitation:** both URLs live in the same `.env`, readable by
    the OS user that runs Core. The role split protects the events
    against Core's own bugs and against a leaked app-role password, not
    against someone who can read `.env` or the disk (the disk is not
    encrypted yet, #57). The triggers stay as a second layer behind the
    grants.
-   Support credential rotation and revocation.
-   Keep development, test, and production credentials separate.

## 6. Prompt injection and untrusted input

External content may contain instructions intended to manipulate an
agent. AETHER must: - Treat external content as untrusted data. - Keep
tool authorization outside the model's control. - Require explicit
policy checks before actions. - Avoid letting retrieved documents change
system instructions. - Restrict which retrieved content can influence
high-impact operations. - Record source references for important claims
and actions.

## 7. Local and cloud data handling

Before sending context to a cloud model: - Classify the data. - Retrieve
only what is necessary. - Apply the user's provider/privacy policy. -
Avoid sending secrets or unrelated personal records. - Record provider
selection and relevant data-handling metadata.

Local inference is not automatically safe: local models and tools still
require permissions, isolation, and audit logging.

**Cloud output cap (#77).** Every cloud model call is capped at
`AETHER_CLOUD_MAX_OUTPUT_TOKENS` output tokens (default 4096; thinking
tokens count toward it). It applies to cloud calls only, not to local
models. A `/chat` request may lower the limit with `max_tokens` but never
raise it (422, input not echoed). The Claude adapter also clamps every
call to the cap, so scripts and other callers can't exceed it. An invalid
value stops Core at startup with an error that names the setting, never
its value. A cut-off answer is returned with `finish_reason` `length`
(possibly with empty text) and recorded in the event; it is never retried
automatically. The cap limits cost per call; the monthly spend limit in
the Anthropic console ($5 per month, set by the owner, docs/00) stays the
hard ceiling, and AETHER does not enforce that limit.

### 7.1 Privacy classification (final, confirmed by the owner 2026-11-26)

Every memory record and every piece of context Core assembles carries
one of these levels. Nexus may only route context to a provider allowed
for the highest level present.

  -----------------------------------------------------------------------
  Level         Meaning                       Allowed providers (default)
  ------------- ----------------------------- ---------------------------
  Public        Already public or harmless    Any configured provider;
                (docs, general questions)     the cloud and an untrusted
                                              Ollama host only with
                                              `allow_cloud`

  Personal      Ordinary preferences, project Local, and a cloud provider
                notes, plans                  or untrusted Ollama host
                                              when the request sets
                                              `allow_cloud` (no
                                              allowlist, #120)

  Sensitive     Health, finances, private     A trusted Ollama host only
                messages, other people's      (loopback or private
                details                       address, or opted in with
                                              `AETHER_OLLAMA_TRUST_HOST`);
                                              as memory, only with the
                                              request's
                                              `include_sensitive`

  Secret        Passwords, API keys, tokens,  Never sent to any model;
                account numbers               never stored in Memory
  -----------------------------------------------------------------------

Unclassified content is treated as Personal. Routing tests must prove
that Sensitive content never reaches a cloud adapter or an untrusted
Ollama host, and that Secret content never reaches any adapter.

In code, the level names and their order are defined once, in
`router/contract.py` (`PrivacyLevel`). The non-Secret subset (identity
`include_by_default`, a provider's `max_privacy`) is derived from it, and
a test fails if any other module lists the names again or if the events
table's CHECK constraint drifts from that order (#58).

The memory store (`memory/store.py`, ADR-024 step 4, #69) refuses
`secret` before the database, whose CHECK on `memory_records` is the
backstop, and its error never echoes the content. A record from a model
or a tool is always `pending` with a suggested level; only the owner can
approve (stating the final level), correct or delete. The store can't
tell who is calling: it trusts the `source` (no default) and `actor` its
caller passes, so the API (step 6 on) must set them from authentication
(ADR-021), never from a request body or model output. The store puts no
memory content or key in log records, error messages or reprs. Each
change writes one metadata-only `memory.*` event (actor `memory`) in the
same transaction: a proposal (`memory.proposed`), an owner create or an
approval (`memory.approved`), a correction (`memory.updated`, with the
old and new level when the level changed) and a delete
(`memory.deleted`). If the change fails no event is written (the expiry
clean-up that may run first is separate, and keeps its own events), and if the
event can't be written the change is rolled back (ADR-024 §5, #72).

Its callers are the memory API, and `/chat` for model proposals (below).
The memory API (`core/memory_api.py`, ADR-024 step 6, #92) has: `GET /memory` (filters `status`, `category`, `privacy` only),
`GET /memory/{id}`, `POST /memory/search` (key and text query in the
body, never the URL, which the access log records), `POST /memory`, and
(step 8, #97) `POST /memory/{id}/approve` and `POST /memory/{id}/reject`,
and (step 9, #101) `PATCH /memory/{id}` and `DELETE /memory/{id}`, and
(step 10, #122) `GET /memory/export`.
Every route is behind the bearer token, which means the owner in Phase 2
(ADR-024 §2), so the API always passes `source="owner"`; the body can't
set `source`, `status`, `privacy_source` or `source_request_id` (extra
fields are a 422). `POST /memory` requires `privacy` (422 without it,
never defaulted) and refuses `secret`. `source_request_id` is Core's
request ID; an incoming `X-Request-ID` or `X-Session-ID` that contains a
configured secret value is never adopted (a new ID is generated), so it
is not echoed, logged, put in an event or stored (#75). Bodies are capped
at 16 KiB. With no database configured or reachable (for example the
encrypted volume locked) every memory route answers 503
`MemoryUnavailable` with a fixed message, never an empty list, a 404 or
a write that did nothing (#80). Approve requires the owner's `privacy`
(the suggested level is never kept by default) and may correct content,
category and key; the secret check runs on the corrected text. If
another approved record has the final key, approving deletes that record
in the same transaction (`memory.deleted`, then `memory.approved`), so
the new value replaces the old one (two approvals with the same key at
the same moment: the second gets 409 `KeyConflict`, nothing lost). Reject hard-deletes a pending record
(`memory.rejected`); an approved record is deleted only with `DELETE`.
`PATCH` corrects `content`, `category`, `key`, `privacy` or
`expires_at`, overwriting the old text with no history (D6); the secret
check runs on the corrected text, a level set there is the owner's, and
one `memory.updated` event records the old and new level when the level
changed. Only `key` and `expires_at` may be sent as `null` (to clear
them); `null` for anything else is a 422, never "unchanged". A key that
another approved record holds is a 409 (only approve replaces), and an
expired record can't be corrected (404). `DELETE` hard-deletes any
record, expired or not, with `memory.deleted` (ADR-024 §4: what remains
in backups is described in §11 below). Setting a key first deletes
expired records, so an expired record the owner can't see never blocks
its key. The store holds at most 50 pending records (D8): one more from
a model or a tool is refused with `PendingLimit`. A pending record
expires 30 days after it was proposed, any record at its `expires_at`;
an expired record is never returned, approved or rejected, and the
clean-up deletes it with one `memory.expired` event, run by the store
before a list or a create at most once a minute (and, unthrottled,
before the owner sets a key), so an expired record
can stay on disk until the next list or create. The clean-up runs as
part of the read: if it fails, the list or create fails too (503 or
500), never an empty or stale answer. A record a restore sets back to
`pending` is marked `restored` (migration 0006): it never expires and
doesn't count toward the 50-proposal limit, until the owner approves or
rejects it (approving clears the marker).

Model proposals (ADR-033, ADR-024 D7, step 15): only when a `/chat`
request sets `propose_memory: true`, and only after an answer that
finished normally (`stop`). Core then makes **one more call to the
provider that answered**, through `call_with_identity` (identity, the #8
tripwire, the echo scrub, `max_privacy`) with the same `allow_cloud`,
**no memory**, and only the **latest user message**: not the answer,
earlier turns or memory, so a proposal can only draw on what the owner
just wrote. A proposal is still model text: it can be wrong or invented,
and outside text pasted into the message can steer it (§6); that is why
it is pending. The call asks for at most 512 output tokens (fewer if the
request's `max_tokens` is lower). The reply is parsed as data: one JSON object
(`{"proposals": [...]}`, one Markdown fence tolerated), each item exactly
`content`, `category`, `privacy`, a known category, never `secret`, at
most 3, no duplicates; anything else is dropped. The stored level is the
higher of the model's suggestion and the request's level, so a model
can't make the owner's text look more public. Records are `pending`,
`source="model"`, with the provider and request ID, through the store's
normal checks (length, `SecretCheck`, 50 pending). A proposal call error,
an unusable reply or a store error never fails the answer: the reply's
`memory_proposals` count is 0, and one log line gives counts and an
error type, never text; records stored before a store failure stay and
are counted. The proposal call has its own `model.call.completed` (or
`provider.unavailable`) event, marked with the fixed `route_reason`
"memory proposal call (ADR-033), not the answer"; a refusal before
sending writes none. No failover.
Limit: secret-shaped text in a format `SecretCheck` doesn't know can be
proposed; it stays pending, is never retrieved, and the owner rejects it.

Retrieval for model calls (ADR-024 §3, step 12): each call, the
failover call included, gets its own selection after its route is
chosen, for the provider that receives it: approved, unexpired records
in `include_by_default` (Public, Personal), Sensitive only with the
request's `include_sensitive`, never Secret, and never above the
provider's `memory_limit` (the cloud: up to Personal, since the
content tripwire (#8) checks memory text too, and nothing without
`allow_cloud`; an untrusted Ollama host: the same as the cloud
(ADR-032); a trusted one: up to
Sensitive). The request sent carries the effective level (the higher of
the request's and its memory's). `call_with_identity` checks every
record again against the provider's own limit and refuses the call
(`MemoryNotAllowed`, nothing sent) if one doesn't fit. Each call that
could get memory writes one `memory.context.retrieved` event (counts,
characters, effective level, record IDs; never content). A provider
error message that contains a whole text sent in the call (a message,
the system prompt, a memory record's content or its rendered line) of
at least 8 characters is replaced before it is logged, stored or
returned (`router/echo.py`). Partial quotes and shorter texts are not
caught; adapters still build messages from safe parts only. Not yet
recorded: `include_sensitive` itself (the per-request approval) is in no
event; only the count of Sensitive records sent is (#144).
`GET /memory/export` returns one JSON document
(`{"format": "aether-memory", "version": 1, "exported_at", "records"}`)
with every column of every unexpired record: all levels, Sensitive
included, and pending records (ADR-024 §4, D10). It is the owner's own
data on the authenticated route; once downloaded the file is outside
AETHER's control, so keep it off synced or shared folders. The response is
`Cache-Control: no-store`. It takes no filters: any query parameter is a
422, so `?privacy=public` can't pass for a filtered file. Expired records are left out by the same filter
as the list; the export deletes nothing and runs no clean-up. One
`memory.exported` event records the count only (no IDs, levels or
content), in the same transaction as the read: if the event can't be
written, no export is returned. Logs carry the count only.
A `key` or `query` put in the `GET /memory` URL by mistake is refused
with 422, and the access log drops the query string of requests to
`/memory` and its sub-paths: it logs `?<query removed>` instead
(`AccessLogQueryFilter`, `core/log.py`, #93). Limits: text put in the
path itself (`GET /memory/<text>`) is still logged, as is the query of a
path that doesn't route to memory (`//memory`, `/MEMORY`, a percent-encoded
`?`), and any path under a uvicorn `--root-path`, which Core doesn't use
(#166).

**Secret-shaped content is never stored (ADR-024 §1, #70).** Whatever its
label, the store refuses content that looks like a secret, on create,
update and approve (including corrected content), before the database is
touched. The check is `SecretCheck` in `core/log.py`, built from the
same things the log redactor masks, defined once (`SECRET_FORMATS` and
`secrets_from()`):

-   Anthropic-style API keys (`sk-ant-...`);
-   `Bearer <credential>` in any casing, with any whitespace, when the
    credential is 8 or more token characters (`Bearer abc123` is not
    refused). The word is also found inside a longer one, so
    "standard-bearer organisation" is refused: the safe direction;
-   a password in a `scheme://user:password@host` URL;
-   the configured secret values (`ANTHROPIC_API_KEY`,
    `AETHER_API_TOKEN`, the database passwords), only when at least 8
    characters long, so a short value can't match ordinary text.

The key, `source_provider` and `source_request_id` are checked too:
their formats rule out the patterns, but a configured value could still
be written there. The error (`SecretContent`, field `content`, or the
other field's name) says only that the value looks like
a secret: never which pattern, which part or the text, with nothing
chained and nothing logged but the operation and field name. Reads are
not checked. `MemoryStore` requires a `SecretCheck` as a constructor
argument, so no store exists without one; the store checks only the
type, so callers must build it with `SecretCheck.from_settings` to
include the configured values.

**Content tripwire for cloud sends (#8).** The privacy level is whatever
the caller says; this is the check on the text itself. Before any cloud
provider is called, `call_with_identity` (`router/identity_call.py`,
the one function every model call goes through) runs the same
`SecretCheck` on every message and on the full system prompt. On a
match nothing is sent, whatever the label: the call raises
`SecretContentRefused`, which is not retryable, so routing never fails
over with it, and `/chat` answers 422 `SecretContentRefused` with a fixed
message that never says which text, pattern or value matched. The
attempt, its error type and the `request.failed` event are recorded,
metadata only. A manual override to the cloud and a failover from local
to the cloud are refused the same way. Any provider kind other than
`local` is checked, so a new kind fails closed, and so is an untrusted
`OLLAMA_HOST` (off-network, ADR-032). Sends to a trusted Ollama host are
not checked, including one trusted with `AETHER_OLLAMA_TRUST_HOST`, which
may be on another machine: the opt-in is the owner's statement of trust. `Router` and `call_with_identity`
require a `SecretCheck` (type-checked); `Router.from_settings` builds it
with `SecretCheck.from_settings`. The check covers each message and the
system prompt, and memory goes into the system prompt, so the memory
sent to the cloud is checked as well; a test pins `ModelRequest`'s
fields, so a new text field must be added to the checked texts, or that
test fails, before it can reach the cloud. The
limits below apply to this check as well: it catches known formats and
configured values, not every secret.

Limit: the check knows only these formats and values, on the literal
text. A secret in any other format (an account number, a password typed
as plain words, a key from another provider) or split up (spaces, line
breaks, zero-width characters) can still be stored if the owner approves it under
a lower label; the owner's approval review is the remaining check.

**Trust boundary: `OLLAMA_HOST` (ADR-023, #10).** "Local only" means
the Ollama host is on this machine or the local network, not just that
the Ollama adapter is used. Core decides this from the URL text, without
resolving DNS, and the Ollama client follows no redirects and ignores
proxy environment variables, so it only talks to the host it checked:

-   Trusted (`max_privacy` `sensitive`): the exact name `localhost`
    (resolved by the OS on connect) and IP literals in
    127.0.0.0/8, ::1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and
    link-local (169.254.0.0/16, fe80::/10).
-   Any other host (a name, a public IP, an overlay or CGNAT address,
    IPv6 unique-local) is **off-network** and treated like the cloud for
    privacy (ADR-032, #22): it takes only requests with `allow_cloud`, at
    most `personal` (Sensitive requests are refused), gets memory only
    with `allow_cloud` and up to Personal, and its sends get the content
    tripwire. It stays `kind="local"`, so with `allow_cloud` it is still
    preferred over the cloud when it has headroom. Core logs one
    fixed-text startup warning that carries no host or other value.
-   `AETHER_OLLAMA_TRUST_HOST=true` (off by default) trusts such a host
    deliberately, for example a private overlay network; Core then logs
    that the opt-in is active.
-   Known limitations: a private-range address is trusted even on a
    network the owner doesn't control; the opt-in is not checked
    further; `allow_cloud` also covers an off-network Ollama host, which
    its name doesn't say; and an https `OLLAMA_HOST` signed by a private CA is not
    trusted, because the client ignores `SSL_CERT_FILE`/`SSL_CERT_DIR`
    and verifies against the certifi bundle, not the system trust store
    (#24).

## 8. Execution isolation

-   Run code tests in a dedicated workspace or container where feasible.
-   Do not run unreviewed generated code with unrestricted host
    privileges.
-   Limit CPU, memory, filesystem, and network access where practical.
-   Use timeouts and cancellation.
-   Capture output and exit status.
-   Require approval before operations outside the allowed workspace.

## 9. Audit and observability

Record: - Who or what initiated an action. - Which task and request it
belongs to. - Which tool and permission were used. - What action
category was performed. - Start time, end time, and outcome. - Approval
decision where applicable. - Evidence or output references.

Avoid storing unnecessary sensitive payloads in audit logs.

## 10. Sentinel independence

**Sentinel v1** is the deterministic checks in GitHub Actions: tests,
lint, type checks and the secret scan (`sentinel.yml`, jobs `checks`
and `secrets`), plus the dependency audit described in §10.2. CodeQL
code scanning also runs on pull requests (GitHub default setup), but it
is not a required check. **AI review is not part of Sentinel.** Today it
is manual and advisory: the `security-reviewer` subagent and
`@codex review` on request (§10.1). An automated AI reviewer in CI stays
Future (docs/04).

-   The authoring agent cannot disable mandatory Sentinel checks.
-   Sentinel findings must be evidence-based.
-   AI reviewers are advisory unless a defined policy gives a specific
    check a blocking role.
-   Critical changes require human approval.
-   Review records are preserved independently from the authoring
    conversation.
-   A failed or skipped check must be visible; it must not be reported
    as passed.

### 10.1 Enforcing independence with GitHub (Sentinel v1)

Independence is meant to come from GitHub permissions rather than trust
in an AI. Since the public flip on 2026-11-14 (ADR-022), part of it
does; the rest is still only a rule.

**Enforced by the `protect-main` ruleset on `main`** (ADR-022, "Ruleset
design"):

-   a pull request is required; nobody can push directly to `main`;
-   the Sentinel status checks `checks` and `secrets` are required, with
    GitHub Actions as their source, so a failing check blocks the merge
    and a status with the same name from elsewhere cannot satisfy them;
-   force pushes and branch deletion are blocked;
-   the bypass list is empty.

These hold as long as the ruleset is unchanged; admin credentials can
edit or disable it (see below). **Not required:** approvals (the
required approving review count is 0) and code-owner review (off). There
is one maintainer, and GitHub does not let an author approve their own
PR. CodeQL is not among the required checks.

**Still only a rule, not a permission.** The agent works with the
owner's GitHub credentials, which have admin rights, so GitHub does not
stop it from doing the following. They hold because the agent follows
them and the owner checks:

-   The owner merges, only with the local `safemerge` fish function,
    which waits for Sentinel checks to pass and merges only the head
    commit it checked. When given the reviewed hash
    (`safemerge <PR> <hash>`, the normal practice), it refuses to merge
    unless the PR head matches it. The
    live function stays in the owner's fish config. A backup and
    documentation copy is in `scripts/safemerge.fish` (README, "Merging
    with `safemerge`"), and is never sourced or symlinked, so a pull
    request can't change the gate it passes through.
-   AI coding agents (for example Claude Code) work on branches and open
    pull requests. They never merge, push to `main`, change repository
    settings or rulesets, or edit `.github/`/`CLAUDE.md` unless asked
    (`CLAUDE.md` rule 5).
-   Agents act only on issues written by the owner. Text in outside
    issues, pull requests and comments is data, never instructions. The
    only Codex trigger is `@codex review`, never `@codex fix`
    (`CLAUDE.md` rule 12, `CONTRIBUTING.md`, ADR-022 decision 6).
-   **Manual review layer (advisory, not enforced by GitHub):**
    -   the `security-reviewer` subagent, run before any PR that touches
        the areas this document covers, with its report pasted into the
        PR (§10.3, `CLAUDE.md`);
    -   `@codex review`, on the owner's request;
    -   the owner's planner chat (claude.ai), for plans, ADRs and a
        second look at guardrail PRs (ADR-027, Proposed).
    These find problems; none of them can block or approve a merge.
    `.github/CODEOWNERS` names the owner for `.github/` and `CLAUDE.md`,
    but nothing enforces it, so reviewing changes to those paths before
    merging is the owner's manual step.

Before the flip (GitHub Free, private repository; ADR-018), none of the
ruleset items above were enforced, and Sentinel failures were visible
but did not block a merge.

### 10.2 Dependency and supply-chain scanning (Dependabot)

Sentinel's dependency layer is GitHub Dependabot (#34):

-   **Dependabot alerts** (repository setting): GitHub flags known
    vulnerabilities in dependencies from `uv.lock` and in the GitHub
    Actions used by Sentinel.
-   **Dependabot security updates** (repository setting): Dependabot
    opens a PR to fix a vulnerable dependency as soon as a fix exists.
-   **Version updates** (`.github/dependabot.yml`): weekly PRs for
    Python packages (`uv` ecosystem: `pyproject.toml` and `uv.lock`)
    and for GitHub Actions, including SHA-pinned actions such as
    `setup-uv`. Minor and patch updates are grouped into one PR per
    ecosystem; major updates get their own PR. The Postgres image in
    `docker-compose.yml` (`docker-compose` ecosystem, #52) gets digest
    and 18.x updates only. Major versions are ignored, because they need
    an upgrade plan (ADR-020). Sentinel tests each update against a live
    database.
-   **Cooldown:** version updates wait 7 days after a release before
    being proposed, so a compromised or broken release has time to be
    noticed and pulled. Security updates are not delayed.

Dependabot PRs are opened by the Dependabot bot, not by an agent or the
owner. They get no repository secrets and a read-only token, and go
through Sentinel and `safemerge` like every other PR. A Dependabot PR
that changes behaviour (a major update or a failing check) is handled
like any other change: read the changelog, fix or close it. Never merge
it on trust.

**Actions pinned by commit SHA (#61).** Every `uses:` in
`.github/workflows/` is pinned to a full 40-character commit SHA, with the
release in a comment (`@<sha> # v7.0.1`), so a moved or compromised tag
can't change what runs. Dependabot's `github-actions` entry updates the
SHA and the comment together. `tests/test_workflow_pins.py` fails on any
tag, branch or short-SHA reference.

**Known-vulnerability scan in Sentinel (#61).** On every push and PR, the
`checks` job exports every locked package from `uv.lock` (runtime and dev)
and checks it with `pip-audit`, which itself comes from the lockfile. Any
finding fails the job. Unlike Dependabot alerts, this blocks the PR that
introduces a vulnerable version. pip-audit sends package names and
versions to PyPI's advisory API; no token is involved.

**An advisory with no fix** fails every PR until it's handled:

1.  Review it: is the vulnerable code path reachable in AETHER? Is a
    workaround or a different package possible? Is upgrading a direct
    dependency enough?
2.  If it must be accepted for now, add `--ignore-vuln <ID>` to the
    pip-audit command in `sentinel.yml`. That is a `.github/` edit, so the
    owner approves it.
3.  Next to it, add a comment with the date, the reason and when to
    recheck, for example:
    `# --ignore-vuln PYSEC-2026-1234: 2026-11-20, not reachable (we never
    call X); recheck when a fix ships`.
4.  Remove the ignore as soon as a fixed version is locked.

**Static analysis of our own code:** ruff's `S` rules (flake8-bandit) run
on every push (#25). GitHub code scanning (CodeQL, SARIF uploads, the
Security tab) isn't available while the repository is private: it needs a
GitHub Code Security license, which needs a Team or Enterprise plan. It is
free for public repositories, so ADR-022 enabled it right after the flip
(2026-11-14).

Not covered (accepted gaps): vulnerabilities in our own code beyond
ruff's `S` rules; malicious packages without a published advisory (only
mitigated by the 7-day cooldown, `uv sync --locked` and hash-pinned
lockfiles); and the local machine's system packages.

### 10.3 Local guardrails (#42)

A layer before Sentinel, on the owner's machine. It catches mistakes
early; it is **not** a security boundary and does not replace Sentinel.

-   **Pre-commit hooks** (`.pre-commit-config.yaml`, installed with
    `uv run pre-commit install`): gitleaks on staged changes,
    `detect-private-key`, `check-added-large-files` (500 KB),
    `check-merge-conflict`, and ruff from `uv.lock` with auto-fix
    (`ruff check --fix`, `ruff format`; a fixed file stops the commit
    for review and re-staging; Sentinel only checks).
    Third-party hooks are pinned to commit SHAs; Dependabot's
    `pre-commit` ecosystem proposes updates weekly (7-day cooldown).
-   **Claude Code permissions** (`.claude/settings.json`, committed):
    -   *deny*: reading or editing `.env`, `.env.local`, `.env.*.local`,
        `.env.prod`, `.env.prod.*` and `.env.dev*` in any folder
        (`.env.example` stays editable; any other new `.env.*` name
        must be added here),
        reading credentials outside the repo (`~/.ssh`, `~/.gnupg`,
        `~/.aws`, `~/.config/gh`, `~/.config/anthropic`), force pushes,
        pushes to `main`, `--no-verify` on commit and push, `rm -rf` and
        its spellings, `gh pr merge`, `gh repo edit/delete/archive`,
        `gh secret`, `gh auth token`.
    -   *ask*: `git reset --hard`, `git clean`, `git branch -D`,
        `git stash drop/clear`, `sudo`, and edits to `.github/**`,
        `CLAUDE.md`, `.claude/settings.json`,
        `.claude/settings.local.json`, `.claude/agents/**`,
        `.pre-commit-config.yaml` and `scripts/safemerge.fish`: allowed
        when the owner approves (the coverage issue and #21 change
        workflows), but an agent cannot quietly loosen its own
        guardrails. `.claude/settings.local.json` is git-ignored, so
        hooks or allow rules added there never show in a diff; the ask
        rule is where an Edit to it surfaces (a shell write is covered
        only by the general Bash prompt, see Limitations) (#84).
    -   Deny wins over ask and allow, in every settings file.
    -   *model*: `"sonnet"` by default; issues labelled `model:opus`
        are started on Opus (rules in `CLAUDE.md`, "Model choice").
-   **Security-reviewer subagent** (`.claude/agents/security-reviewer.md`,
    runs on Sonnet by default, on Opus when the PR touches `.claude/`,
    `CLAUDE.md`, `.github/`, the pre-commit config, this document, or
    code handling secrets, permissions or routing): read-only review of `git diff main...HEAD`, plus
    uncommitted changes (`git status`, `git diff HEAD`), against this
    document and the Accepted ADRs, run before opening any PR that
    touches the areas this document covers. Its report says whether
    the working tree was dirty, and the report is pasted into the PR
    description ("Security review" section of the PR template) so the
    reviewer outside the session sees it (#84). Its tool list includes
    Bash, so "read-only" is an instruction in its prompt, not an
    enforced limit; the deny rules above still apply to it.
-   **PR template** (`.github/pull_request_template.md`): checklist for
    tests run (output pasted), Sentinel, security impact, docs, known
    limitations and rollback notes.

**Limitations:**

-   `git commit --no-verify` (outside Claude Code) skips the hooks, and
    hooks only run in clones where `pre-commit install` was run.
-   Permission rules match Claude Code's own tool calls and command
    patterns. A command they don't anticipate (a script, another
    spelling, an interpreter one-liner) can still read a file or delete
    one. The real protections remain: secrets only in `.env`, log
    redaction, gitleaks in Sentinel, and the owner merging.
-   Claude Code reads the settings when a session starts; a session
    that started before `.claude/` existed needs a restart.

### 10.4 Public repository

The repository is public (ADR-022). Everything committed, including in
branches, pull requests, issues and commit messages, can be read and
copied by anyone, and stays in history. Never commit:

-   memory data, or anything taken from the database or a backup;
-   `.env` or any secret (§5);
-   logs, test output or exit-check answers that came from real use;
-   personal details: names, addresses, health, finances, private
    messages, or other people's details.

Use invented test data (for example the marker words in the Phase 1 exit
check). Sentinel's secret scan and the pre-commit hooks catch secret
shapes only; they cannot recognise personal data.

## 11. Backups and recovery

-   Define backup scope and retention.
-   Encrypt backups where appropriate.
-   Test restore procedures, not just backup creation.
-   AETHER's database (ADR-025; #3): `core.backup` runs
    `pg_dump` inside the Postgres container over its local socket, so no
    password is used or passed. Files are written under a temporary name,
    verified (the table of contents, then a full read of every data
    block), then renamed; the folder is private (700, owned by the user,
    files 600) and outside the repository, and never a cloud location
    (a policy: the code can't tell a synced folder). Retention is 7
    daily plus 4 weekly (ADR-024 D5); the newest backup is never
    pruned, and a failed run prunes nothing. Failures exit non-zero with
    one log line holding no path, URL, password or tool output. The
    daily timer runs from a second clone that only ever has `main`
    checked out and has no `.env`, and refuses to start otherwise, so
    the timer never runs an unreviewed branch with the owner's Docker
    access (ADR-025 D8).
-   **Encryption at rest (#57, ADR-026).** The Postgres data and the
    backups move into one LUKS2 container file (20 GB, ext4 inside),
    unlocked only by a passphrase the owner types. It is never in `.env`,
    a keyfile on this disk, compose, a script or a unit. The owner carried
    out the move by hand on 2026-11-27 (`docs/runbooks/encrypted-volume.md`
    stages 0–5, results on #57): since then the database and the nightly
    backups are on the volume, which the owner opens with `aether-vault
    open` after each boot. Both hold the events and full memory records
    (content, key, level and provenance). With #1, #12 and #3 already
    done, this meets the ADR-024 §7 gate for storing real memory data,
    **provided every compose run loads the override until stage 7**
    (#152): a run without it (`-f docker-compose.yml`, another folder, a
    lost `.env` line) starts the old database on the unencrypted
    `aether_pgdata` volume, and real memory written there can't be wiped
    from this disk (ADR-026 §5 layer 6, §7). Stage 7, removing the old
    unencrypted volume and backups and rebinding that name to the
    volume, is still open (#152, not before 2026-12-04, with the owner's
    approval).
    -   **Fails closed, with the override loaded** (`compose.vault.yaml`,
        through `COMPOSE_FILE` in `.env` or `aether-vault`). With the volume
        locked or not mounted, Docker refuses to start Postgres: the bind
        source exists only inside the volume, `create_host_path: false`,
        and the bare mount point is empty and immutable. The entrypoint
        also refuses without the volume's marker.
    -   A compose run that misses the override uses the old named volume
        until the runbook's stage 7 recreates that name as a bind to the
        volume. After that, it can't create a database on the bare disk
        either. The nightly backup refuses to run (exit 1, one `err`
        journal line). Core still starts with events skipped, `/ready`
        reports `degraded` with database `unreachable`, the memory routes
        answer 503, and `/chat` answers without memory.
    -   **Encrypted:** the Postgres data, the backups and psql history in
        the container (`PSQL_HISTORY`), while the volume is locked or the
        PC is off.
    -   **Not encrypted:**
        -   anything while the volume is open;
        -   swap on disk and hibernation images;
        -   core dumps;
        -   memory text typed into shell commands (shell history);
        -   data written before the move (old volume, old backups,
            snapshots). Deleting it is not wiping it on btrfs or an SSD,
            which is acceptable only because, before the move, it holds
            event metadata and invented memory data only.
        -   memory sent or copied out on purpose: to model providers by
            retrieval (local Ollama, whose logs can hold prompts with
            debug logging on; the cloud or an off-network Ollama host
            with `allow_cloud`), the export file wherever it is saved
            (§7.1: keep it off synced or shared folders), and `/memory`
            output in terminal scrollback or Claude Code transcripts
            (ADR-026 §8).
    -   Postgres's error log lives in Docker's log on the unencrypted
        disk. With the volume's override it no longer carries DETAIL
        lines (rows, keys) or the failing statement
        (`log_error_verbosity=terse`, `log_min_error_statement=panic`).
        Some primary messages can still quote a fragment of input.
-   **Known limitation:** backups sit on the **same physical disk** as the
    database, so they don't protect against losing the disk. An encrypted
    off-machine copy is a separate follow-up. A forgotten passphrase loses
    the database and its backups together; the LUKS header backup is kept
    off the machine (ADR-026 §3).
-   **Deletion and backups:** a deleted memory record stays in every
    backup taken before the delete, until retention prunes the last of
    them, about 5 weeks with daily backups (ADR-024 §4). A restore brings
    back the past; the runbook (`docs/runbooks/db-backup-restore.md`)
    restores into a new database first, checks row counts, triggers and
    grants (`scripts/restore_check.sql`), repairs memory in the copy
    from the live database's events (`memory/restore.py`: deletes again,
    sets records changed since back to `pending` at the higher level,
    marked `restored`), and replaces the real database only by renaming,
    keeping the old one. The safety backup taken before
    a restore and the kept old database are outside retention and must
    be deleted by hand within 5 weeks, or a deleted record survives in
    them.
-   Roles are not in a backup. A restore onto a new cluster recreates
    `aether_app` and sets its password from `.env`; the dump brings back
    its grants. Owner and role password verifiers are never backed up.
-   Record destructive operations.
-   Use version control and snapshots for recoverable project changes.
-   Provide a way to stop or revoke active agent sessions and
    credentials.

## 12. Security acceptance checklist

Before a feature is considered ready: - \[ \] Permissions are explicit
and least-privilege. - \[ \] Secrets are not hard-coded or logged. - \[
\] Untrusted content cannot grant itself permissions. - \[ \] Write
operations are scoped and auditable. - \[ \] Consequential actions
require the appropriate approval. - \[ \] Timeouts and cancellation
exist for long-running work. - \[ \] Failure behavior is documented. -
\[ \] Relevant security tests pass. - \[ \] User-facing status
accurately reports what happened.
