# ADR-028: OpenRouter as an OpenAI-compatible provider adapter

**Status:** Proposed (build after Phase 2 exit criteria pass)\
**Date:** 2026-11-26\
**Owner:** BoxDelta

## Context

Nexus (ADR-003) routes between Ollama and Claude. Reaching more models
with separate vendor adapters means more accounts, keys and code.
OpenRouter exposes many models behind one OpenAI-compatible API. Memory
stays provider-independent (ADR-004), so any model receives the same
retrieved context. docs/04 lists extra adapters as Future; this ADR
records the design so it can be built later.

## Options

1.  Separate adapters per vendor (Gemini, Groq, Mistral).
2.  One OpenAI-compatible adapter pointed at OpenRouter.
3.  Do nothing.

## Decision

Option 2, after Phase 2 exit criteria pass.

-   One adapter in `router/` with a fixed base URL
    (`https://openrouter.ai/api/v1`), not freely configurable, and an
    explicit `api_key` argument. The host is checked at startup, and the
    key is never sent to any other host. It never falls back to
    `OPENAI_API_KEY`.
-   Core runs the same secret-shaped-content check Memory uses
    (`SecretCheck`, docs/05 §7.1) on outbound context, and blocks the
    request on a match.
-   `max_tokens` is set on every request, with the value in config
    (`AETHER_CLOUD_MAX_OUTPUT_TOKENS`, docs/05 §7).
-   Every request sets `provider.zdr=true`, `data_collection=deny` and
    `allow_fallbacks=false`, and names an explicit model. OpenRouter's
    own auto-routing is not used. Nexus chooses and logs the route and,
    where the response reports it, the host that served it.
-   If no compliant host exists the request fails visibly. Nexus may fall
    back to another provider only if the privacy level allows it.
-   Privacy levels (docs/05 §7.1): Public only at first. Personal is
    enabled only after routing tests pass and the owner approves in a
    later ADR update. Sensitive and Secret never reach it.
-   `OPENROUTER_API_KEY` lives in `.env` and its name goes in
    `.env.example`. The owner sets a spend limit in OpenRouter before the
    first request.

### Validation before building (docs/03 §8)

Confirm current OpenRouter terms, logging settings and ZDR coverage;
confirm cost limits; confirm the adapter can be removed without touching
Core or Memory.

## Consequences

-   \+ Many models with one key; the same memory for all of them.
-   \- One more company handles prompts besides the model host.
-   \- ZDR-only routing shrinks the model list; some requests will fail.
-   \- Model names and prices change; tests must not depend on a live
    model.
-   \- Routing tests must treat this adapter as a cloud adapter.

## Revisit when

ZDR coverage is too narrow; terms change; Personal-level use is proposed;
a direct vendor adapter becomes preferable.
