# ADR-040: Local model via llama-swap

**Status:** Proposed\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

Aether's local provider is the Ollama adapter (`router/ollama.py`,
`llama3.1:8b`). The dispatcher prototype (ADR-030) already runs
Qwen3.6-35B-A3B at Q4_K_M under llama-swap in Docker on `127.0.0.1:9292`,
with an API key. ADR-030's fold-in calls for an OpenAI-compatible local
adapter in `router/` with ADR-023's host-trust rules, and #154 asks for
the same.

Measured on the PC (`tests/local-model/README.md`):

-   **VRAM:** with `--n-cpu-moe 33` and a 16k context, about 1.1 GiB
    stayed free. With the chosen `--n-cpu-moe 35`, about 1.9 GiB (1,930
    MiB) stays free while the model is loaded.
-   **Speed:** with `--n-cpu-moe 35`, about 41 tok/s generation and
    about 314 tok/s prompt reading (a 2,118-token prompt). The first
    request takes about 25 s while the model loads.
-   **Thinking:** must be off on every request, or replies come back
    empty.
-   **RAM:** about 18 GB stays available with Postgres, n8n and Plane
    running. zram absorbs about 4 GB while the model loads. The CPU
    scheduler stays responsive; the owner still has to check how the
    desktop feels.
-   **Identity continuity:** it keeps the name, its tone is closer to
    the identity, and it follows the no-false-action rule that
    llama3.1:8b breaks.
-   **Dispatcher triage:** 10 of 11 correct.
-   **Routing:** Aether's GPU probe sees 1,131 MiB free with the model
    loaded with `--n-cpu-moe 33`, below the 1,536 MiB reserve. As
    written today, routing avoids local exactly while the model is warm.
    With `--n-cpu-moe 35` the free VRAM (about 1,930 MiB) is above the
    reserve, but the probe still counts the model's own allocation (see
    the GPU probe decision), so a different GPU load can't hide it.

Constraints:

-   LiteLLM and Langfuse on this PC log full prompts and answers.
    Aether's context can be Personal or Sensitive (docs/05 §7.1, §9), so
    Aether must not send through them.
-   One 8 GB GPU is shared with the desktop and gaming. The Charter says
    to keep at least 1.5 GB free.

## Options

1.  Keep Ollama and run Qwen there. Rejected: Ollama has no
    `--n-cpu-moe` control, so the MoE split that makes the model fit
    isn't available.
2.  Reach llama-swap through LiteLLM, as the dispatcher does. Rejected:
    LiteLLM and Langfuse log full prompts, and Aether's memory context
    would end up in their stores.
3.  Use the OpenRouter adapter (ADR-028) for llama-swap too. Rejected:
    ADR-028 is a cloud and off-network provider with cloud rules
    (`allow_cloud`, `public` memory). Keeping local and cloud adapters
    separate keeps the trust rules simple.
4.  **A new local adapter for llama-swap's OpenAI-compatible API**
    (chosen).

## Decision

Option 4.

-   **Adapter:** a new `router/` provider for llama-swap's
    OpenAI-compatible `/v1/chat/completions`, `kind="local"`.
    -   It uses the existing `Provider` contract. Every call goes through
        `call_with_identity`, as with every provider.
    -   Base URL, model name and API key come from `.env`. The names go in
        `.env.example` with no values. The key is added to the log
        redactor like the others.
    -   The default base URL is loopback, `http://127.0.0.1:9292`.
-   **Direct only:** Aether talks to llama-swap directly, never through
    LiteLLM or Langfuse. A base URL that points at LiteLLM's port is not
    a supported setup; the runbook says so.
-   **Thinking off on every request.** The adapter always sends
    `chat_template_kwargs: {"enable_thinking": false}`. A reply with
    empty content is a `ProviderBadResponse`, never an empty answer
    (tested).
-   **Host trust:** the same rules as Ollama (ADR-023, ADR-032).
    -   A loopback or private host is trusted: memory up to the
        provider's limit, `sensitive` only with `include_sensitive`.
    -   Any other host is untrusted and off-network: it needs
        `allow_cloud` and gets `public` memory only.
    -   The adapter doesn't share code paths with the OpenRouter adapter
        (ADR-028).
    -   The HTTP client follows no redirects and ignores proxy
        environment variables (`follow_redirects=False`,
        `trust_env=False`), as ADR-023 requires; tested.
    -   An untrusted host goes through the same `needs_allow_cloud(info)`
        helper as an untrusted Ollama host, so the content tripwire
        (ADR-032) still runs although the adapter is `kind="local"`. It
        gets the "[off-network host: data leaves this network]" route
        mark and the fixed-text startup warning. No trust opt-in like
        `AETHER_OLLAMA_TRUST_HOST` is offered for this adapter.
    -   The key is also covered by `SecretCheck`'s configured values, so
        it can't be stored as memory. Bearer auth over plain HTTP to a
        private-LAN host has the same limit as Ollama.
    -   docs/05 §7.1 is updated with these rules before this ADR is
        Accepted.
-   **One model, one request:** Aether and the dispatcher share one
    loaded model and send one request at a time to begin with (docs/03
    §3). The dispatcher reaches it through LiteLLM as today. That
    carries no Aether context, because it is a separate client (ADR-030).
    More parallel requests come only after measuring them.
-   **GPU probe:** the probe counts the model's own allocation.
    -   NVML lists `llama-server` with its memory, so free VRAM plus that
        allocation is the room the local model already has.
    -   Local routing is not blocked by the model it would use, and other
        GPU users are still counted as before.
    -   When the model is not loaded, the check is the same as today:
        enough free VRAM for the model plus the reserve, otherwise
        cloud.
    -   The probe change is tested with fakes.
-   **llama-server flags:** `--jinja -ngl 99 --n-cpu-moe 35 -c 16384
    -fa on --cache-type-k q8_0 --cache-type-v q8_0` (D1).
-   **Idle unload:** llama-swap's `ttl`, set in the dispatcher stack
    outside this repo, is 900 s (it was 300 s). After the last request
    the model holds the GPU for up to 15 minutes (about 1.9 GiB stays
    free), then unloads. The value applies to the dispatcher too, and
    dispatcher traffic also resets the timer, so Aether can't guarantee
    when the GPU is free. Aether itself never keeps the model warm: no
    keep-alive pings, and the probe and `/ready` don't call an endpoint
    that loads it.
-   **Cold start in the chat UI:** when the model is cold, the chat view
    shows "Waking local model…" until the first token arrives (Phase 3
    Step 4, docs/08).
-   **Ollama:** the Ollama adapter stays, disabled by default for
    Phase 3, so it can be switched back on as a fallback. It is removed
    after Phase 3, with its own issue.
-   **Placement:** this is part of the ADR-030 fold-in track. It lands
    before Phase 3 Step 4 (chat view, docs/08), so daily use starts on
    the new model.

## Decisions for the owner

-   **D1. VRAM reserve while the model is loaded: decided.** Use
    `--n-cpu-moe 35`. Re-measured on 2026-11-27 (details in
    `tests/local-model/README.md` §3):
    -   free VRAM about 1,930 MiB while loaded, above the 1.5 GB
        Charter reserve;
    -   generation about 41 tok/s, prompt reading about 314 tok/s;
    -   `ttl` raised to 900 s.
-   **D2. Desktop feel: passed.** A 5,000-token reply (about 2 minutes,
    40.0 tok/s) ran with the services running. The owner used the
    desktop normally and reported no problems (2026-11-27). Details in
    `tests/local-model/README.md` §3. If this changes later, use a
    higher `--n-cpu-moe` or a smaller context.

## Consequences

-   Easier: one stronger local model for Aether and the dispatcher;
    better identity continuity; no prompts in the logging stack.
-   Harder: two local runtimes exist until Ollama is removed. llama-swap
    becomes a dependency, as a Docker image outside this repository.
    Its config lives in the dispatcher stack.
-   Risks:
    -   A cold first request takes about 25 s, so the chat view shows
        "Waking local model…" until the first token.
    -   The 16k context limits how much memory context and chat history
        fit.
    -   llama-swap and llama-server logs sit on the unencrypted disk;
        the runbook says verbose request logging stays off. Memory
        prompts and dispatcher prompts share one llama-server slot,
        acceptable because both run only on this PC.
    -   If someone points the base URL at LiteLLM by mistake, prompts get
        logged. The runbook and a startup warning for a known LiteLLM
        port reduce that risk.

## Implementation (later PRs, not in this ADR's PR)

1.  The adapter, its settings and `.env.example` names, redaction,
    contract tests with fakes (thinking off, empty reply, timeouts, host
    trust), and a `/ready` component.
2.  The GPU probe counts the model's own allocation (tests with fakes).
3.  Ollama disabled by default; routing prefers the llama-swap
    provider.
4.  An identity continuity run through the adapter
    (`scripts/identity_continuity.py` gains the provider).
5.  Docs: docs/01 §4.3, docs/05 §7, docs/03, CODE_TOUR, QUICK_REF.

## Revisit when

-   The GPU changes, the model no longer fits with the reserve, or
    measured parallel use needs more than one request at a time.
-   Ollama is removed (after Phase 3).
