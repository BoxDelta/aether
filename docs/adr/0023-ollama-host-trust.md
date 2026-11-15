# ADR-023: Treat Ollama as local only on a loopback or private host

**Status:** Proposed (owner to accept)\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

docs/05 §7.1 says Sensitive content stays local. Until now "local" meant
"the Ollama adapter": it was `kind="local"` with `max_privacy="sensitive"`
by class, wherever `OLLAMA_HOST` pointed. A remote or https
`OLLAMA_HOST` would have received Sensitive data silently (#10).

## Decision

1.  The Ollama provider may receive Sensitive content only when
    `OLLAMA_HOST` is trusted. Trust is decided from the URL text alone;
    DNS is never resolved (a name could resolve differently later).
2.  Trusted: `localhost`, and IP literals in 127.0.0.0/8, ::1,
    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and link-local
    (169.254.0.0/16, fe80::/10). Everything else is untrusted: other
    names, public IPs, CGNAT/overlay ranges such as 100.64.0.0/10, IPv6
    unique-local (fc00::/7), IPv4-mapped IPv6 and `0.0.0.0`.
3.  An untrusted host caps the provider at `max_privacy="personal"`.
    Sensitive requests are refused for it (and, with no other allowed
    provider, get `NoRoute`). Core logs one warning at startup with the
    host category only, never the host value.
4.  `AETHER_OLLAMA_TRUST_HOST=true` is an explicit opt-in, off by
    default, for an owner who deliberately trusts another host (for
    example a private overlay network). It restores `sensitive` and
    logs a warning (category only) that the opt-in is active.
5.  The provider stays `kind="local"`, so routing rules are unchanged:
    a Personal request can still go to an untrusted Ollama host without
    `allow_cloud`.

Options considered: resolving DNS and checking the addresses (rejected:
DNS can change after startup, and a lookup is itself a leak); refusing
to start with an untrusted host (rejected: blocks a legitimate remote
setup for Public and Personal use); treating an untrusted host as
`kind="cloud"` (not chosen now: it would change routing order and the
`allow_cloud` meaning; see "Revisit when").

## Consequences

-   Easier: "Sensitive stays local" now holds for where the host is,
    not only for which adapter is used. Tested.
-   Harder: an owner with a remote GPU over an overlay network must set
    the opt-in to keep Sensitive requests working.
-   Risks: a private-range address is trusted even if it belongs to a
    network the owner doesn't control (for example public Wi-Fi). The
    opt-in trusts the host the owner names, with no further check.
    Personal data still goes to an untrusted Ollama host without
    `allow_cloud`.

## Revisit when

Memory starts sending Personal context automatically; Core runs on more
than one machine; or an untrusted host should be treated like cloud
(`allow_cloud` required).
