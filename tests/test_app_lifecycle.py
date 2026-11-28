"""Shutdown closes provider HTTP clients and the DB pool (#22 review)."""

import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from core.app import create_app
from core.config import Settings
from core.events import EventRecorder
from core.identity import load_identity
from core.log import configure_logging
from router.claude import ClaudeProvider
from router.contract import ModelRequest, ModelResponse, Provider, ProviderInfo
from router.llama_swap import LlamaSwapProvider
from router.ollama import OllamaProvider
from router.routing import Router
from tests.fakes import CHECK

FAKE_KEY = "sk-ant-api03-FAKE-lifecycle-0123456789"  # gitleaks:allow


class Closable(Provider):
    def __init__(self, name: str, *, fail: bool = False) -> None:
        super().__init__(
            ProviderInfo(name=name, kind="local", default_model="m", max_privacy="sensitive")
        )
        self.closed = 0
        self.fail = fail

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def check_available(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed += 1
        if self.fail:
            raise RuntimeError(f"close failed {FAKE_KEY}")


def track_dispose(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record AsyncEngine.dispose() calls (patched on the class: instances use slots)."""
    disposed: list[bool] = []
    original = AsyncEngine.dispose

    async def tracked(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(True)
        await original(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", tracked)
    return disposed


def test_ollama_adapter_closes_its_http_client() -> None:
    provider = OllamaProvider("http://127.0.0.1:11434", "llama3.1:8b")
    inner = provider._client._client  # the SDK's httpx.AsyncClient
    assert not inner.is_closed

    asyncio.run(provider.aclose())

    assert inner.is_closed


def test_claude_adapter_closes_its_client() -> None:
    provider = ClaudeProvider(SecretStr(FAKE_KEY), "claude-opus-5-5")
    assert provider._client is not None and not provider._client.is_closed()

    asyncio.run(provider.aclose())

    assert provider._client.is_closed()


def test_claude_adapter_without_key_closes_cleanly() -> None:
    asyncio.run(ClaudeProvider(None, "claude-opus-5-5").aclose())


def test_router_closes_every_provider_even_if_one_fails() -> None:
    out = io.StringIO()
    configure_logging(Settings(_env_file=None), stream=out)
    first, broken, last = Closable("a"), Closable("b", fail=True), Closable("c")
    router = Router([first, broken, last], load_identity(), secret_check=CHECK)

    asyncio.run(router.aclose())

    assert (first.closed, broken.closed, last.closed) == (1, 1, 1)
    [warning] = [json.loads(x) for x in out.getvalue().splitlines() if "close failed" in x.lower()]
    assert warning["provider"] == "b"
    assert warning["error_type"] == "RuntimeError"
    assert FAKE_KEY not in out.getvalue()


def test_app_shutdown_closes_injected_router_providers() -> None:
    provider = Closable("ollama")
    router = Router([provider], load_identity(), secret_check=CHECK)
    app = create_app(Settings(_env_file=None), router=router, events=EventRecorder(None))

    with TestClient(app):
        assert provider.closed == 0

    assert provider.closed == 1


def test_app_shutdown_closes_real_adapters_and_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+asyncpg://aether:not-a-real-pw-789@127.0.0.1:1/aether"
    settings = Settings(
        _env_file=None,
        anthropic_api_key=SecretStr(FAKE_KEY),
        database_url=SecretStr(url),
        llama_swap_model="qwen-test",
        aether_ollama_enabled=True,
    )
    app = create_app(settings)
    llama_swap, ollama, claude = app.state.router._providers
    assert isinstance(llama_swap, LlamaSwapProvider)
    assert isinstance(ollama, OllamaProvider) and isinstance(claude, ClaudeProvider)
    disposed = track_dispose(monkeypatch)

    with TestClient(app):
        pass

    assert llama_swap._client.is_closed
    assert ollama._client._client.is_closed
    assert claude._client is not None and claude._client.is_closed()
    assert disposed == [True]


def test_engine_disposed_even_if_router_close_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenRouter(Router):
        async def aclose(self) -> None:
            raise RuntimeError("router close bug")

    url = "postgresql+asyncpg://aether:not-a-real-pw-790@127.0.0.1:1/aether"
    settings = Settings(_env_file=None, database_url=SecretStr(url))
    app = create_app(
        settings, router=BrokenRouter([Closable("x")], load_identity(), secret_check=CHECK)
    )
    disposed = track_dispose(monkeypatch)

    with pytest.raises(RuntimeError, match="router close bug"), TestClient(app):
        pass

    assert disposed == [True]


# --- Core refuses the owner role (#1, #12) ------------------------------------------


def test_app_refuses_same_role_as_migrations() -> None:
    from core.db import SAME_ROLE_MESSAGE, PrivilegedRoleError

    url = "postgresql+asyncpg://aether:not-a-real-pw-791@127.0.0.1:1/aether"
    settings = Settings(
        _env_file=None, database_url=SecretStr(url), database_migration_url=SecretStr(url)
    )
    with pytest.raises(PrivilegedRoleError) as info:
        create_app(settings)
    assert str(info.value) == SAME_ROLE_MESSAGE


def test_app_refuses_to_start_when_the_role_is_privileged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.app
    from core.db import PRIVILEGED_MESSAGE, DbStatus, PrivilegedRoleError

    async def privileged(*_: object) -> DbStatus:
        return DbStatus(False, "misconfigured", PRIVILEGED_MESSAGE)

    monkeypatch.setattr(core.app, "check_database", privileged)
    url = "postgresql+asyncpg://aether_app:not-a-real-pw-792@127.0.0.1:1/aether"
    app = create_app(
        Settings(_env_file=None, database_url=SecretStr(url)),
        router=Router([Closable("x")], load_identity(), secret_check=CHECK),
    )
    disposed = track_dispose(monkeypatch)

    with pytest.raises(PrivilegedRoleError), TestClient(app):
        pass  # pragma: no cover - startup must fail

    assert disposed == [True]  # the pool is still closed


def test_app_starts_when_the_database_is_down() -> None:
    url = "postgresql+asyncpg://aether_app:not-a-real-pw-793@127.0.0.1:1/aether"
    app = create_app(
        Settings(_env_file=None, database_url=SecretStr(url)),
        router=Router([Closable("x")], load_identity(), secret_check=CHECK),
    )
    with TestClient(app) as client:  # unreachable is not a reason to refuse
        assert client.get("/health").status_code == 200
