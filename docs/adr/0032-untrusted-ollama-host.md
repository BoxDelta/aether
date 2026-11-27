# ADR-032: An untrusted Ollama host needs allow_cloud, like the cloud

**Status:** Proposed (2026-11-27). Amends ADR-023 §5; decides ADR-024 D4's
follow-up and #22.\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

ADR-023 caps an untrusted `OLLAMA_HOST` (not loopback, not a private or
link-local IP literal, no `AETHER_OLLAMA_TRUST_HOST`) at `personal`, but
keeps it `kind="local"`. Three things follow, and Phase 2 makes them matter:

1.  **Requests.** A Personal request goes to that host without
    `allow_cloud`, although the data leaves the machine and the network
    (#22). Unclassified requests are Personal, so this is the default case.
2.  **Memory.** ADR-024 D4 limits such a host to `public` memory until #22
    is decided. Personal memory (the owner's name, preferences) is
    withheld from it even when the owner would accept it, and there is no
    way to say so per request.
3.  **Tripwire.** The #8 content tripwire checks cloud sends only. An
    untrusted host is `local`, so a secret-shaped text reaches it
    unchecked (docs/05 §7.1 notes this as part of #22).

The cloud already has the right shape for all three: nothing without
`allow_cloud`, Personal at most, memory up to Personal, tripwire on.

## Options

-   **A. Treat an untrusted host like the cloud for privacy, not for
    routing order (recommended).** The provider stays `kind="local"`, so
    it is still preferred when it has headroom. New on `ProviderInfo`:
    `off_network: bool`, set by the Ollama adapter from `host_category`
    (true only for an untrusted host without the opt-in), false for every
    other provider. Where the code now asks "is this cloud?" for a privacy
    reason, it asks "is this cloud or off-network?":
    -   routing: an off-network provider is eligible only with
        `allow_cloud` (otherwise `NoRoute`, or the trusted provider);
    -   `memory_limit_for`: no memory without `allow_cloud`;
        `memory_limit="personal"` for it, as for the cloud (D3);
    -   tripwire: checked like a cloud send.
    `AETHER_OLLAMA_TRUST_HOST=true` keeps its meaning: the owner trusts
    the host, so none of this applies.
    Trade-off: one flag, one meaning ("may this request leave my
    network?"). A remote-GPU setup without the opt-in needs
    `allow_cloud` on every request, which is the point.
-   **B. A separate per-request flag (`allow_remote`).** Same checks, but
    keyed on a new flag. Trade-off: finer control, but two flags to set and
    explain, and a client that sets only `allow_cloud` gets a surprise.
-   **C. Make an untrusted host `kind="cloud"`.** Simplest code change.
    Trade-off: it changes routing order (local is preferred, cloud is the
    fallback), the cloud output cap and `provider_kind` in events, for a
    provider that is still a local model. Rejected in ADR-023 for that
    reason.
-   **D. Keep today's rules and rely on the startup warning.** Trade-off:
    no work, but Personal requests keep leaving the network by default,
    and memory stays public-only for that host.

## Decision

Option A, if the owner accepts it. Implementation is step 13 of #58, one
PR:

1.  `ProviderInfo.off_network: bool = False`; a validator refuses
    `off_network=True` with `kind="cloud"` (it would mean nothing) and
    with `max_privacy` above `personal`.
2.  `OllamaProvider` sets it from `host_category` and the opt-in.
3.  `Router.decide`, `choose_route` (`allow_cloud` rule), `memory_limit_for`
    and the tripwire in `call_with_identity` use one helper,
    `needs_allow_cloud(info)` (cloud or off-network). New provider kinds
    still fail closed (anything not explicitly local needs it).
4.  Ollama's `memory_limit` on an untrusted host becomes `personal`
    (reached only with `allow_cloud`), replacing D4's `public`.
5.  Docs: ADR-023 §5 and "Revisit when" point here; docs/05 §7.1; README
    trust paragraph; `docs/CODE_TOUR.md`.
6.  Tests: the routing table for untrusted hosts with and without
    `allow_cloud`; memory per host category × `allow_cloud`; the tripwire
    on an untrusted host; the opt-in leaves all of it off.

## Consequences

-   Easier: "nothing leaves my network unless I said `allow_cloud`" holds
    for every provider, and memory follows the same rule.
-   Harder: a remote Ollama host without the opt-in can't answer a request
    that lacks `allow_cloud`; such requests get `NoRoute` if nothing else
    is available. The startup warning should say so.
-   Risks: `allow_cloud` now also means "an untrusted Ollama host", which
    the name doesn't say. The API docs must state it. A private-range
    address on a network the owner doesn't control is still trusted
    (ADR-023's risk, unchanged).

## Revisit when

Another provider can be off-network (for example OpenRouter, ADR-028, which is
cloud anyway), or Core runs on more than one machine.
