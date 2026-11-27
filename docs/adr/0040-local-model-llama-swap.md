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

-   **VRAM:** `--n-cpu-moe 33` with a 16k context uses about 7.4 GB.
    NVML shows `llama-server` holding 6.0 GB, and about 1.1 GiB stays
    free.
-   **Speed:** about 42 tok/s generation and about 350 tok/s prompt
    reading. The first request takes about 25 s while the model loads.
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
    loaded, below the 1,536 MiB reserve. As written today, routing
    avoids local exactly while the model is warm.

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
-   **Idle unload:** llama-swap's `ttl` (300 s today) unloads the model
    when idle, so the GPU is free for gaming. Aether never keeps it
    warm on purpose: no keep-alive pings, and the probe and `/ready`
    don't load it.
-   **Ollama:** the Ollama adapter stays, disabled by default for
    Phase 3, so it can be switched back on as a fallback. It is removed
    after Phase 3, with its own issue.
-   **Placement:** this is part of the ADR-030 fold-in track. It lands
    before Phase 3 Step 4 (chat view, docs/08), so daily use starts on
    the new model.

## Decisions for the owner

-   **D1. The VRAM reserve while the model is loaded.** With
    `--n-cpu-moe 33` about 1.1 GiB stays free, below the Charter's 1.5 GB.
    -   Recommended: raise `--n-cpu-moe` until 1.5 GiB stays free (about
        35–36, between the measured 34 = 6979 MiB and 40 = 4390 MiB),
        and re-measure tok/s.
    -   Alternative: accept about 1.1 GiB while it is loaded, and rely on
        the 300 s `ttl` to free the GPU for gaming. That amends the
        Charter target.
    -   Trade-off: speed against desktop headroom.
-   **D2. Desktop feel.** The owner checks by hand that the desktop
    stays usable while a long reply is generating, with the services
    running. If it doesn't, use a higher `--n-cpu-moe` or a smaller
    context, whatever D1 chose.

## Consequences

-   Easier: one stronger local model for Aether and the dispatcher;
    better identity continuity; no prompts in the logging stack.
-   Harder: two local runtimes exist until Ollama is removed. llama-swap
    becomes a dependency, as a Docker image outside this repository.
    Its config lives in the dispatcher stack.
-   Risks:
    -   A cold first request takes about 25 s, so the chat view must show
        that it is loading.
    -   The 16k context limits how much memory context and chat history
        fit.
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
