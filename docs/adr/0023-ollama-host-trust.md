# ADR-023: Treat Ollama as local only on a loopback or private host

**Status:** Accepted (owner, 2026-11-15)\
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
    the classifier never resolves DNS (a name could resolve differently
    later). The Ollama HTTP client follows no redirects and ignores proxy
    environment variables, so requests go only to the host that was
    checked.
2.  Trusted: the exact name `localhost` (not `localhost.`), and IP
    literals in 127.0.0.0/8, ::1,
    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and link-local
    (169.254.0.0/16, fe80::/10). Everything else is untrusted: other
    names, public IPs, CGNAT/overlay ranges such as 100.64.0.0/10, IPv6
    unique-local (fc00::/7), IPv4-mapped IPv6 and `0.0.0.0`.
3.  An untrusted host caps the provider at `max_privacy="personal"`.
    Sensitive requests are refused for it (and, with no other allowed
    provider, get `NoRoute`). Core logs one fixed-text warning at
    startup that carries no host or other value; the category is kept on
    the provider (`host_category`).
4.  `AETHER_OLLAMA_TRUST_HOST=true` is an explicit opt-in, off by
    default, for an owner who deliberately trusts another host (for
    example a private overlay network). It restores `sensitive` and
    logs a fixed-text warning that the opt-in is active.
5.  The provider stays `kind="local"`, so routing rules are unchanged:
    a Personal request can still go to an untrusted Ollama host without
    `allow_cloud` (#22). **Amended by ADR-032 (2026-11-27):** an untrusted
    host is off-network and needs `allow_cloud`, like the cloud.

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
-   Risks: `localhost` is trusted by name and resolved by the OS when
    the client connects. A private-range address is trusted even if it belongs to a
    network the owner doesn't control (for example public Wi-Fi). The
    opt-in trusts the host the owner names, with no further check.
    Personal data still goes to an untrusted Ollama host without
    `allow_cloud` (#22). **Amended by ADR-032:** no longer; it needs
    `allow_cloud`. Because the client ignores the environment, an
    https `OLLAMA_HOST` signed by a private CA is not trusted:
    `SSL_CERT_FILE`/`SSL_CERT_DIR` are ignored and httpx verifies
    against the certifi bundle, not the system trust store (#24).

## Revisit when

Memory starts sending Personal context automatically; Core runs on more
than one machine; or an untrusted host should be treated like cloud
(`allow_cloud` required): done in ADR-032 (2026-11-27).
