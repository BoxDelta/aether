"""OLLAMA_HOST trust boundary (#10, ADR-023, docs/05 §7.1)."""

import asyncio
import logging
import socket
from typing import Any

import httpx
import pytest
from pydantic import AnyHttpUrl

from core.config import Settings
from core.identity import Identity
from router.contract import (
    Message,
    ModelRequest,
    PrivacyLevel,
    PrivacyNotAllowed,
    ProviderBadResponse,
)
from router.hardware import GpuState, HardwareSnapshot
from router.ollama import OllamaProvider, classify_host
from router.routing import NoRoute, RouteOptions, Router

MODEL = "llama3.1:8b"
GIB = 1024**3
UNTRUSTED_HOST_MARKER = "gpu-box.example.org"


def handler(seen: list[httpx.Request]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": MODEL, "name": MODEL}]})
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "created_at": "2026-11-14T08:00:00Z",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "done_reason": "stop",
            },
        )

    return httpx.MockTransport(respond)


def provider(host: str, seen: list[httpx.Request], **kw: Any) -> OllamaProvider:
    return OllamaProvider(host, MODEL, transport=handler(seen), **kw)


def request(privacy: PrivacyLevel) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content="Hi"),), privacy=privacy)


@pytest.mark.parametrize(
    ("host", "category"),
    [
        ("http://localhost:11434", "loopback"),
        ("http://LOCALHOST:11434", "loopback"),
        ("http://localhost.:11434", "hostname"),  # only the exact name is trusted
        ("http://127.0.0.1:11434", "loopback"),
        ("http://127.8.9.10:11434", "loopback"),
        ("http://[::1]:11434", "loopback"),
        ("http://10.1.2.3:11434", "private"),
        ("http://172.16.0.1:11434", "private"),
        ("http://172.31.255.254:11434", "private"),
        ("http://192.168.1.20:11434", "private"),
        ("http://169.254.10.10:11434", "link_local"),
        ("http://[fe80::1]:11434", "link_local"),
        ("http://[fe80::1%25eth0]:11434", "link_local"),
        # Not trusted without the opt-in:
        ("http://gpu-box:11434", "hostname"),
        ("https://ollama.example.com", "hostname"),
        ("http://localhost.example.com:11434", "hostname"),
        ("http://127.0.0.1.nip.io:11434", "hostname"),
        ("http://172.32.0.1:11434", "other_ip"),
        ("http://8.8.8.8:11434", "other_ip"),
        ("http://100.64.0.1:11434", "other_ip"),  # CGNAT / overlay networks
        ("http://0.0.0.0:11434", "other_ip"),
        ("http://[::ffff:127.0.0.1]:11434", "other_ip"),
        ("http://[fd00::1]:11434", "other_ip"),
        ("http://[2001:db8::1]:11434", "other_ip"),
    ],
)
def test_classify_host(host: str, category: str) -> None:
    assert classify_host(host) == category


def test_classification_never_resolves_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_dns(*_: object, **__: object) -> None:
        raise AssertionError("DNS lookup attempted")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(socket, "gethostbyname", no_dns)

    assert classify_host(f"http://{UNTRUSTED_HOST_MARKER}:11434") == "hostname"
    assert provider(f"http://{UNTRUSTED_HOST_MARKER}:11434", []).info.max_privacy == "personal"


@pytest.mark.parametrize(
    "host", ["http://localhost:11434", "http://[::1]:11434", "http://192.168.1.20:11434"]
)
def test_trusted_host_allows_sensitive(host: str) -> None:
    seen: list[httpx.Request] = []
    p = provider(host, seen)

    assert p.info.max_privacy == "sensitive"
    asyncio.run(p.generate(request("sensitive")))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "host", [f"https://{UNTRUSTED_HOST_MARKER}", "http://8.8.8.8:11434", "http://100.64.0.1:11434"]
)
def test_untrusted_host_is_capped_at_personal(host: str) -> None:
    seen: list[httpx.Request] = []
    p = provider(host, seen)

    assert p.info.kind == "local"
    assert p.info.max_privacy == "personal"
    with pytest.raises(PrivacyNotAllowed):
        asyncio.run(p.generate(request("sensitive")))
    assert seen == []  # nothing was sent
    asyncio.run(p.generate(request("personal")))
    assert len(seen) == 1


def test_untrusted_host_warning_is_metadata_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="aether.router.ollama")

    p = provider(f"https://{UNTRUSTED_HOST_MARKER}:11434", [])

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "capped at personal" in record.getMessage()
    assert "host_category" not in record.__dict__  # fixed text, no extra fields
    assert p.host_category == "hostname"
    assert all(UNTRUSTED_HOST_MARKER not in str(r.__dict__) for r in caplog.records)
    assert UNTRUSTED_HOST_MARKER not in caplog.text


def test_trusted_host_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="aether.router.ollama")

    provider("http://localhost:11434", [])

    assert caplog.records == []


def test_opt_in_trusts_another_host(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="aether.router.ollama")
    seen: list[httpx.Request] = []

    p = provider("http://100.64.0.1:11434", seen, trust_host=True)

    assert p.info.max_privacy == "sensitive"
    asyncio.run(p.generate(request("sensitive")))
    assert len(seen) == 1
    [record] = caplog.records
    assert "AETHER_OLLAMA_TRUST_HOST" in record.getMessage()
    assert p.host_category == "other_ip"
    assert "100.64.0.1" not in str(record.__dict__)
    assert "100.64.0.1" not in caplog.text


def test_redirect_is_not_followed() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://collector.example.net/api/chat"})

    p = OllamaProvider("http://127.0.0.1:11434", MODEL, transport=httpx.MockTransport(respond))

    with pytest.raises(ProviderBadResponse):
        asyncio.run(p.generate(request("sensitive")))
    assert [str(q.url.host) for q in seen] == ["127.0.0.1"]


def test_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.net:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.net:3128")

    p = OllamaProvider("http://127.0.0.1:11434", MODEL)

    inner = p._client._client
    assert inner.trust_env is False
    assert inner.follow_redirects is False


@pytest.mark.parametrize(
    ("raw", "host", "category"),
    [
        ("http://127.1:11434", "127.0.0.1", "loopback"),
        ("http://2130706433:11434", "127.0.0.1", "loopback"),
        ("http://0x7f.0.0.1:11434", "127.0.0.1", "loopback"),
        ("http://127.0.0.1.:11434", "127.0.0.1", "loopback"),
        ("http://LOCALHOST:11434", "localhost", "loopback"),
        ("http://localhost.:11434", "localhost.", "hostname"),
        ("http://[::1]:11434", "::1", "loopback"),
        (f"https://{UNTRUSTED_HOST_MARKER}", UNTRUSTED_HOST_MARKER, "hostname"),
    ],
)
def test_classifier_and_client_see_the_same_host(raw: str, host: str, category: str) -> None:
    # Production input goes through AnyHttpUrl, which normalises odd IPv4 spellings;
    # the classifier and the SDK must agree on the normalised host.
    p = OllamaProvider.from_settings(Settings(_env_file=None, ollama_host=AnyHttpUrl(raw)))

    assert p.host_category == category
    assert p._client._client.base_url.host == host


# --- settings ----------------------------------------------------------------------


def test_opt_in_is_off_by_default() -> None:
    assert Settings(_env_file=None).aether_ollama_trust_host is False


@pytest.mark.parametrize(
    ("raw", "expected"), [("", False), ("  ", False), ("false", False), ("true", True)]
)
def test_opt_in_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("AETHER_OLLAMA_TRUST_HOST", raw)
    assert Settings(_env_file=None).aether_ollama_trust_host is expected


@pytest.mark.parametrize(("trust", "level"), [(False, "personal"), (True, "sensitive")])
def test_from_settings_applies_opt_in(trust: bool, level: str) -> None:
    settings = Settings(
        _env_file=None,
        ollama_host=AnyHttpUrl(f"https://{UNTRUSTED_HOST_MARKER}"),
        aether_ollama_trust_host=trust,
    )
    assert OllamaProvider.from_settings(settings).info.max_privacy == level


def test_default_settings_host_is_trusted() -> None:
    assert OllamaProvider.from_settings(Settings(_env_file=None)).info.max_privacy == "sensitive"


# --- routing ---------------------------------------------------------------------


def probe() -> HardwareSnapshot:
    return HardwareSnapshot(
        gpu=GpuState("nvml", 8 * GIB, 4 * GIB, 10), ram_available_bytes=16 * GIB
    )


def router(host: str, seen: list[httpx.Request], identity: Identity) -> Router:
    return Router([provider(host, seen)], identity, probe=probe)


def test_sensitive_is_refused_for_untrusted_host(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    r = router(f"https://{UNTRUSTED_HOST_MARKER}", seen, identity)

    with pytest.raises(NoRoute):
        asyncio.run(r.handle(request("sensitive"), RouteOptions(allow_cloud=True)))
    assert [q.url.path for q in seen if q.url.path == "/api/chat"] == []


def test_sensitive_still_routes_locally_for_loopback(identity: Identity) -> None:
    seen: list[httpx.Request] = []
    r = router("http://127.0.0.1:11434", seen, identity)

    result = asyncio.run(r.handle(request("sensitive"), RouteOptions()))

    assert result.decision.provider == "ollama"
    assert [q.url.path for q in seen if q.url.path == "/api/chat"] == ["/api/chat"]
