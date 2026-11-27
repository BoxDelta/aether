# Code tour

A one-page map for a developer opening AETHER for the first time. It
describes the code as it is on `main` (Phase 1). For the reasons behind
the design, follow the ADR numbers into `docs/adr/`.

## What each folder does

-   `core/`: the FastAPI service ("Core"). The app factory (`app.py`),
    settings (`config.py`), bearer-token auth (`auth.py`), request IDs
    (`request_context.py`), JSON logging, secret redaction and the shared
    "looks like a secret" check `SecretCheck` (`log.py`),
    the shared error format and 422 handler (`errors.py`), the `/chat`
    endpoint (`chat.py`), `/ready` (`ready.py`), the memory routes
    (`memory_api.py`: list, get, search, owner create, approve,
    reject, correct, delete and export), the Postgres
    engine and its privileged-role check (`db.py`; the app role may update
    and delete only in `memory_records`), the owner-only step
    that sets the app role's password (`db_admin.py`), verified backups
    with D5 retention (`backup.py`), the append-only
    event log (`events.py`; memory events go through `write_memory_event`
    on the caller's connection, in the same transaction) and the
    identity loader and system-prompt builder (`identity.py`).
-   `router/`: "Nexus", which picks a model and calls it. The provider
    contract and privacy levels (`contract.py`), the routing rules and
    failover (`routing.py`), the GPU/RAM probe (`hardware.py`), the one
    function every model call goes through (`identity_call.py`, which also
    refuses a cloud send whose text looks like a secret, #8), and the two
    adapters: `ollama.py` (local) and `claude.py` (cloud).
-   `identity/`: `aether.yaml`, the single AETHER identity (name, voice,
    rules) that is applied to every model call (docs/07).
-   `migrations/`: Alembic migrations for Postgres, run as the owner role
    (`DATABASE_MIGRATION_URL`). `0002_events.py` creates the events table
    and the triggers that make it append-only. `0003_app_role.py` creates
    `aether_app`, the role Core connects as (`DATABASE_URL`): insert and
    read on `events`. `0004_memory_records.py` creates the
    `memory_records` table (ADR-024 §1: content, key, category, privacy
    never `secret`, status, provenance, timestamps, each checked by the
    database) and gives the app role read, insert, update and delete on
    it, nothing more. `0005_memory_events.py` adds the `memory.*` event
    types and their typed, content-free columns to `events` (ADR-024
    §5), widens `actor` to `memory` and `restore`, and changes no grants;
    its downgrade refuses while memory events exist.
-   `memory/`: the memory store (`store.py`, ADR-024 step 4) and its
    fixed value sets (`types.py`: categories, statuses, sources).
    `MemoryStore` creates, gets, lists, corrects, approves and
    hard-deletes `memory_records` through Core's app engine. Its checks
    run before the database: never `secret`; owner records approved at
    once, model and tool records always `pending` with a suggested level;
    only the owner may approve, update or delete (the store trusts the
    `source` and `actor` its caller passes; `source` has no default);
    content, key and
    one-approved-record-per-key give store errors that never quote
    content or keys. Every change writes one `memory.*` event in the same
    transaction (ADR-024 step 5). Content or a key that looks like a
    secret is refused on create, update and approve by `SecretCheck`
    (`core/log.py`: the log redactor's own patterns and configured
    values), which the store takes as a required constructor argument
    (#70). Its only caller is the memory API (`core/memory_api.py`, step
    6), which builds it once with `SecretCheck.from_settings`, always as
    the owner. With the database down every operation raises
    `StoreUnavailable` and the API answers 503 (#80). Approve replaces an
    approved record with the same key; reject deletes a pending one; at
    most 50 pending; expired records (pending after 30 days, or past
    `expires_at`) are hidden, then deleted by `expire`, which `list` and
    `create` run at most once a minute, and unthrottled before the owner
    sets a key (steps 8 and 9). `export` reads every unexpired record
    (all levels, pending too) and writes one `memory.exported` event with
    the count in the same transaction; it deletes nothing (step 10).
    `retrieval_candidates` reads approved, unexpired records (keyed
    profile/preference records plus full-text matches) at or below a
    level, at most 100, with no event. `retrieval.py` then decides, as a
    pure function, which of them one call may get (privacy rules,
    ordering, at most 20 records and 4000 characters) and renders them
    for `memory_context`; each provider sets `ProviderInfo.memory_limit`
    (step 11). Nothing calls it from `/chat` yet (step 12).
-   `scripts/`: the identity continuity run (`identity_continuity.py`), a
    backup copy of the owner's `safemerge` fish function (never sourced),
    the systemd user timer templates for backups (`systemd/`, installed by
    hand) and the restore check (`restore_check.sql`). The backup command
    itself is `core/backup.py` (`uv run python -m core.backup`, ADR-025).
-   `tests/`: pytest, one file per module or behaviour (`test_routing.py`,
    `test_chat.py`, …). `conftest.py` clears AETHER's environment variables
    for every test. Live Postgres tests run only with `AETHER_DB_TESTS=1`.
-   `docs/`: the specs (00–07), ADRs and runbooks.

## The path of a `POST /chat` request

1.  **Request ID.** `RequestContextMiddleware` (`core/request_context.py`)
    gives the request an ID, which appears in logs, events and the
    response. It also turns any unhandled error into a generic 500 with no
    details.
2.  **Auth.** `BearerAuth` (`core/auth.py`) checks `Authorization: Bearer
    <AETHER_API_TOKEN>` in constant time before any body is read. With no
    valid token configured, every route except `/health` answers 503: Core
    never runs open (ADR-021).
3.  **Size limit.** `BodySizeLimit` (`core/chat.py`) caps the raw body
    before it is parsed.
4.  **Validation and the privacy label.** `ChatRequest` (`core/chat.py`)
    validates the JSON. The caller sets `privacy`
    (public / personal / sensitive / secret); if they don't, it defaults to
    `personal`. `allow_cloud` defaults to `false`. Validation errors never
    echo the input (`validation_error_handler`, `core/errors.py`). The
    handler `chat()` turns the body into a `ModelRequest`
    (`router/contract.py`) and records a `request_received` event.
5.  **Routing.** `Router.handle()` → `Router.decide()`
    (`router/routing.py`):
    -   `secret` is refused before anything else;
    -   only providers allowed to take this privacy level are considered
        (`Provider.allows()`; cloud only with `allow_cloud`);
    -   availability is checked and, for local, the GPU headroom
        (`router/hardware.py`);
    -   `choose_route()` applies the ordered rules in the module docstring
        and returns a `RouteDecision` with a human-readable reason, or
        raises `NoRoute`.
6.  **Identity and the provider call.** `call_with_identity()`
    (`router/identity_call.py`) sets the system prompt to the identity
    prompt (the caller's own system text becomes its task section), logs
    one metadata-only `Model call` record and calls `Provider.generate()` (`router/contract.py`), which
    checks the privacy level again, applies the timeout and turns any
    failure into a `ProviderError` with a safe message. The adapter
    (`OllamaProvider` or `ClaudeProvider`) makes the HTTP call. Ollama may
    receive Sensitive data only on a loopback or private host (ADR-023).
    Claude calls are capped at `AETHER_CLOUD_MAX_OUTPUT_TOKENS`.
7.  **Failover.** If the call fails with a retryable error, `handle()`
    asks `decide()` once more without that provider, so every rule still
    applies to the fallback. A refusal or a cut-off answer is not an
    error and never fails over.
8.  **Response.** `chat()` returns `ChatResponse` with the text, the
    provider, the route reason and token counts, or the shared error shape
    from `core/errors.py` (`RouteFailed` 502, `NoRoute` 503) that carries no
    exception text. Auth (401/503), the size limit (413) and validation
    (422) use the same shape.
9.  **Events.** After the response is sent, a background task
    (`EventRecorder.record()` → `EventWriter`, `core/events.py`) writes the
    events to Postgres: received, route selected, any failed attempt,
    completed or failed. Events hold metadata only, never prompt or answer
    text. Free text is redacted and blanked if it contains the request's
    own text. Core writes them as the app role `aether_app`, which can't
    update, delete or truncate them (and Core refuses to start as the
    owner). Without `DATABASE_URL`, events are skipped with one startup
    warning, and a failed write never affects the request. The events are
    built by two reporting hooks on `Router.handle()` (`on_decision`,
    `on_attempt_failed`). If building one raises, routing goes on, that one
    event is missing, and a metadata-only "Decision callback failed" or
    "Attempt callback failed" warning is logged.

## Where to start reading

1.  `core/app.py`: `create_app()` shows how everything is wired, in about 60
    lines.
2.  `core/chat.py`: the module docstring lists the safety rules, and
    `chat()` is the whole request handler.
3.  `router/routing.py`: the module docstring lists the routing and failover
    rules in order. `choose_route()` is pure and easy to test.
4.  `router/contract.py`: the types every layer shares (`ModelRequest`,
    `ModelResponse`, `Provider`, the privacy levels and errors).
5.  `docs/05_Security_and_Permissions.md` §5 and §7.1: the rules the code
    above enforces.

To see it run: `tests/test_chat.py` drives `/chat` end to end with fake
providers, and `tests/test_routing.py` has a table of routing cases.
