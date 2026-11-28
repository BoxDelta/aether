# Runbook: local model via llama-swap (ADR-040)

Core talks to llama-swap's OpenAI-compatible API directly. llama-swap itself
runs in the dispatcher stack, outside this repository.

## Set up

-   In `.env` (never commit it): `LLAMA_SWAP_URL` (default
    `http://127.0.0.1:9292`), `LLAMA_SWAP_MODEL` (the llama-swap model
    name) and `LLAMA_SWAP_API_KEY`.
-   Restart Core. Without `LLAMA_SWAP_MODEL` the provider is not built and
    `/ready` shows `llama_swap: not_configured`.
-   Ollama stays off unless `AETHER_OLLAMA_ENABLED=true`. To fall back to
    it, set that and restart; routing still prefers llama-swap when both
    are up.

## Rules

-   **Never point `LLAMA_SWAP_URL` at LiteLLM or Langfuse.** They log full
    prompts and answers, and Aether's context can be Personal or Sensitive.
    Core warns on port 4000 (LiteLLM's usual port) but can't tell every
    setup.
-   **Keep verbose request logging off** in llama-swap and llama-server
    (their logs sit on the unencrypted disk). Don't start llama-server with
    options that print prompts.
-   Use a loopback URL. Any other host, a private-LAN address included, is
    treated like the cloud (needs `allow_cloud`, at most Personal, Public memory only) and there is
    no opt-in.
-   Aether never keeps the model warm: no pings; `/ready` only lists models and
    routing only reads `/running` to tell loaded from cold (a model it can't
    ask about counts as cold). llama-swap's `ttl` (900 s) unloads it after the last request,
    dispatcher traffic included.

- Check once on the host that reading `/running` does not reset llama-swap's
  `ttl` (it should not; Aether never keeps the model warm).

## Check

```fish
curl -s http://127.0.0.1:8000/ready -H "authorization: Bearer $token" | jq .components.llama_swap
```

Expect `"ok"`. A cold first chat request takes about 25 s while the model
loads.

Identity continuity through the adapter (owner's approval first):

```fish
uv run python -m scripts.identity_continuity llama-swap
```
