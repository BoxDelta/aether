"""Which hosts count as "this machine or this network" for a local provider.

One classification for the local adapters (Ollama, ADR-023; llama-swap, ADR-040).
The host is classified from the URL text alone; DNS is never resolved. Ollama
trusts the categories below; llama-swap trusts only ``loopback`` (ADR-040). Trusted
for Ollama:
``localhost`` and literal IPs in 127.0.0.0/8, ::1, 10.0.0.0/8, 172.16.0.0/12,
192.168.0.0/16 and link-local (169.254.0.0/16, fe80::/10). Anything else (a name, a
public IP, an overlay-network address) is not trusted.
"""

import ipaddress
from typing import Literal

import httpx

HostCategory = Literal["loopback", "private", "link_local", "hostname", "other_ip"]

_TRUSTED_NETWORKS: dict[HostCategory, tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = {
    "loopback": (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")),
    "private": (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ),
    "link_local": (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10")),
}
TRUSTED_CATEGORIES: frozenset[HostCategory] = frozenset(_TRUSTED_NETWORKS)


def classify_host(host_url: str) -> HostCategory:
    """Category of the host in an Ollama URL, from its text only (no DNS lookup).

    Only the exact name ``localhost`` counts as trusted (not ``localhost.``); every
    other name is ``hostname``. ``localhost`` is still resolved by the OS on connect.
    IPv4-mapped IPv6 and other forms outside the listed ranges are ``other_ip``.
    """
    host = httpx.URL(host_url).host.lower()
    if host == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return "hostname"
    for category, networks in _TRUSTED_NETWORKS.items():
        if any(address.version == n.version and address in n for n in networks):
            return category
    return "other_ip"
