"""GET /ready (#59): status words only, bounded time, behind the token."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

import core.db
import core.ready
from core.app import create_app
from core.config import Settings
from core.db import DbStatus
from core.events import EventRecorder
from core.identity import load_identity
from core.ready import Components, ReadyResponse, migration_head, overall
from router.contract import ModelRequest, ModelResponse, Provider, ProviderInfo, ProviderKind
from router.routing import Router
from tests.fakes import CHECK

TOKEN = "fake-api-token-ready-tests-0123456789abcdef"  # noqa: S105  # gitleaks:allow
AUTH = {"authorization": f"Bearer {TOKEN}"}
FAKE_KEY = "sk-ant-api03-FAKE-ready-0123456789"  # gitleaks:allow
DB_PASSWORD = "not-a-real-ready-pw-31337"  # noqa: S105  # gitleaks:allow
DB_URL = f"postgresql+asyncpg://aether:{DB_PASSWORD}@127.0.0.1:1/aether_ready_db"


class Checked(Provider):
    def __init__(
        self,
        name: str,
        kind: ProviderKind,
        available: bool = True,
        *,
        delay_s: float = 0.0,
        error: Exception | None = None,
        off_network: bool = False,
    ) -> None:
        super().__init__(
            ProviderInfo(
                name=name,
                kind=kind,
                default_model="m",
                max_privacy="sensitive" if kind == "local" and not off_network else "personal",
                off_network=off_network,
            )
        )
        self.available = available
        self.delay_s = delay_s
        self.error = error

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def check_available(self) -> bool:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self.available


def make_client(
    *providers: Provider,
    database_url: str | None = None,
    token: str | None = TOKEN,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        anthropic_api_key=SecretStr(FAKE_KEY),
        database_url=SecretStr(database_url) if database_url else None,
        aether_api_token=SecretStr(token) if token else None,
    )
    identity = load_identity()
    app = create_app(
        settings,
        identity,
        router=Router(list(providers), identity, secret_check=CHECK),
        events=EventRecorder(None),
    )
    return TestClient(app, headers=AUTH, raise_server_exceptions=False)


def ollama(available: bool = True, **kw: Any) -> Checked:
    return Checked("ollama", "local", available, **kw)


def claude(available: bool = True, **kw: Any) -> Checked:
    return Checked("claude", "cloud", available, **kw)


@pytest.fixture
def db_ok(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Database reachable; alembic_version answers are taken from the returned list."""
    answers: list[Any] = ["alembic_version", migration_head()]

    async def fake_check(settings: Settings, engine: Any = None) -> DbStatus:
        # The real message names user@host:port/db; /ready must not return it.
        return DbStatus(True, "ok", "connected to aether@127.0.0.1:1/aether_ready_db")

    class Conn:
        async def scalar(self, statement: Any) -> Any:
            return answers.pop(0)

    class Engine:
        @asynccontextmanager
        async def connect(self) -> AsyncIterator[Conn]:
            yield Conn()

        async def dispose(self) -> None:
            pass

    monkeypatch.setattr(core.ready, "check_database", fake_check)
    monkeypatch.setattr(core.db, "create_engine", lambda settings: Engine())
    return answers


def assert_safe(text: str) -> None:
    for forbidden in (
        FAKE_KEY,
        TOKEN,
        DB_PASSWORD,
        "postgresql",
        "127.0.0.1",
        "aether_ready_db",
        "Traceback",
        "boom",
    ):
        assert forbidden not in text, forbidden


# --- overall status -------------------------------------------------------------------


def test_everything_up_is_ready(db_ok: list[Any]) -> None:
    r = make_client(ollama(), claude(), database_url=DB_URL).get("/ready")

    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "components": {
            "ollama": "ok",
            "llama_swap": "not_configured",
            "claude": "configured",
            "database": "ok",
        },
    }
    assert_safe(r.text)


def test_no_claude_key_is_degraded(db_ok: list[Any]) -> None:
    r = make_client(ollama(), claude(available=False), database_url=DB_URL).get("/ready")

    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["components"]["claude"] == "not_configured"


def test_no_database_is_degraded() -> None:
    r = make_client(ollama(), claude()).get("/ready")

    assert r.status_code == 200
    assert r.json() == {
        "status": "degraded",
        "components": {
            "ollama": "ok",
            "llama_swap": "not_configured",
            "claude": "configured",
            "database": "not_configured",
        },
    }


@pytest.mark.parametrize("down", [ollama(available=False), None], ids=["unavailable", "absent"])
def test_ollama_down_is_not_ready(down: Provider | None, db_ok: list[Any]) -> None:
    providers = [p for p in (down, claude()) if p is not None]
    r = make_client(*providers, database_url=DB_URL).get("/ready")

    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["components"]["ollama"] == ("unavailable" if down else "not_configured")


def test_off_network_ollama_is_not_ready_for_default_requests(db_ok: list[Any]) -> None:
    # ADR-032: up, but it answers only requests with allow_cloud.
    r = make_client(ollama(off_network=True), claude(), database_url=DB_URL).get("/ready")

    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["components"]["ollama"] == "off_network"


@pytest.mark.parametrize(
    ("ollama_s", "claude_s", "database_s", "expected"),
    [
        ("ok", "configured", "ok", "ready"),
        ("ok", "not_configured", "ok", "degraded"),
        ("ok", "configured", "unreachable", "degraded"),
        ("ok", "configured", "not_migrated", "degraded"),
        ("ok", "timeout", "ok", "degraded"),
        ("unavailable", "configured", "ok", "not_ready"),
        ("off_network", "configured", "ok", "not_ready"),
        ("timeout", "configured", "ok", "not_ready"),
        ("error", "configured", "ok", "not_ready"),
    ],
)
def test_overall_rules(ollama_s: str, claude_s: str, database_s: str, expected: str) -> None:
    components = Components(ollama=ollama_s, claude=claude_s, database=database_s)  # type: ignore[arg-type]
    assert overall(components) == expected


# --- llama-swap (ADR-040) -------------------------------------------------------------


def llama_swap(available: bool = True, **kw: Any) -> Checked:
    return Checked("llama-swap", "local", available, **kw)


def test_llama_swap_alone_is_ready_for_default_requests(db_ok: list[Any]) -> None:
    r = make_client(llama_swap(), claude(), database_url=DB_URL).get("/ready")

    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert r.json()["components"]["llama_swap"] == "ok"
    assert r.json()["components"]["ollama"] == "not_configured"


def test_llama_swap_down_with_ollama_up_is_still_ready(db_ok: list[Any]) -> None:
    r = make_client(llama_swap(False), ollama(), claude(), database_url=DB_URL).get("/ready")

    assert r.status_code == 200
    assert r.json()["components"]["llama_swap"] == "unavailable"


def test_no_local_provider_up_is_not_ready(db_ok: list[Any]) -> None:
    r = make_client(llama_swap(False), claude(), database_url=DB_URL).get("/ready")

    assert r.status_code == 503 and r.json()["status"] == "not_ready"


def test_off_network_llama_swap_is_not_ready_for_default_requests(db_ok: list[Any]) -> None:
    r = make_client(llama_swap(off_network=True), claude(), database_url=DB_URL).get("/ready")

    assert r.status_code == 503
    assert r.json()["components"]["llama_swap"] == "off_network"


def test_ready_check_never_asks_llama_swap_to_generate() -> None:
    # check_available() is the only call /ready makes; _generate would raise here.
    r = make_client(llama_swap(), claude()).get("/ready")

    assert r.json()["components"]["llama_swap"] == "ok"


# --- database states --------------------------------------------------------------------


def test_database_not_migrated_when_version_behind(db_ok: list[Any]) -> None:
    db_ok[1] = "0001"
    r = make_client(ollama(), claude(), database_url=DB_URL).get("/ready")
    assert r.json()["components"]["database"] == "not_migrated"


def test_database_not_migrated_without_alembic_table(db_ok: list[Any]) -> None:
    db_ok[:] = [None]
    r = make_client(ollama(), claude(), database_url=DB_URL).get("/ready")
    assert r.json()["components"]["database"] == "not_migrated"


def test_migration_head_read_from_files() -> None:
    assert migration_head() == "0008"


def test_unreachable_database_shows_category_only(capsys: pytest.CaptureFixture[str]) -> None:
    # Real check_database and asyncpg against a closed port: nothing listens on 1.
    r = make_client(ollama(), claude(), database_url=DB_URL).get("/ready")

    assert r.json()["components"]["database"] == "unreachable"
    assert r.json()["status"] == "degraded"
    assert_safe(r.text)
    assert DB_PASSWORD not in capsys.readouterr().out


def test_malformed_database_url_is_misconfigured_without_echo() -> None:
    url = f"mysql://aether:{DB_PASSWORD}@127.0.0.1/aether_ready_db"
    r = make_client(ollama(), claude(), database_url=url).get("/ready")

    assert r.json()["components"]["database"] == "misconfigured"
    assert_safe(r.text)


# --- bounded time and errors ----------------------------------------------------------


def test_hanging_checks_time_out(monkeypatch: pytest.MonkeyPatch, db_ok: list[Any]) -> None:
    monkeypatch.setattr(core.ready, "CHECK_TIMEOUT_S", 0.2)
    client = make_client(ollama(delay_s=30), claude(delay_s=30), database_url=DB_URL)

    start = time.monotonic()
    r = client.get("/ready")
    elapsed = time.monotonic() - start

    assert elapsed < 2.0  # checks run concurrently, each bounded
    assert r.status_code == 503
    assert r.json()["components"] == {
        "ollama": "timeout",
        "llama_swap": "not_configured",
        "claude": "timeout",
        "database": "ok",
    }


def test_check_exception_is_error_with_type_only(capsys: pytest.CaptureFixture[str]) -> None:
    secret_error = RuntimeError(f"boom {FAKE_KEY} {DB_URL}")
    client = make_client(ollama(error=secret_error), claude(error=secret_error))
    capsys.readouterr()

    r = client.get("/ready")

    assert r.json()["components"]["ollama"] == "error"
    assert r.json()["components"]["claude"] == "error"
    assert_safe(r.text)
    out = capsys.readouterr().out
    warnings = [json.loads(x) for x in out.splitlines() if "Readiness check failed" in x]
    assert sorted((w["component"], w["error_type"]) for w in warnings) == [
        ("claude", "RuntimeError"),
        ("ollama", "RuntimeError"),
    ]
    assert "boom" not in out and FAKE_KEY not in out and DB_PASSWORD not in out


def test_response_model_admits_only_status_words() -> None:
    with pytest.raises(ValidationError):
        Components(ollama="ok", claude="configured", database="connected to aether@host")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ReadyResponse.model_validate(
            {
                "status": "ready",
                "components": {
                    "ollama": "ok",
                    "llama_swap": "not_configured",
                    "claude": "configured",
                    "database": "ok",
                },
                "detail": DB_URL,
            }
        )


# --- access -------------------------------------------------------------------------------


def test_ready_needs_the_token() -> None:
    client = make_client(ollama(), claude())

    assert client.get("/ready", headers={"authorization": ""}).status_code == 401
    assert client.get("/ready", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/health", headers={"authorization": ""}).status_code == 200


def test_ready_refused_when_no_token_configured() -> None:
    r = make_client(ollama(), claude(), token=None).get("/ready")
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "AuthNotConfigured"


def test_health_stays_liveness_only() -> None:
    r = make_client(ollama(available=False)).get("/health", headers={"authorization": ""})
    assert r.status_code == 200
    assert set(r.json()) == {"status", "version"}


def test_router_without_claude_reports_not_configured() -> None:
    r = make_client(ollama()).get("/ready")
    assert r.json()["components"]["claude"] == "not_configured"
    assert r.json()["status"] == "degraded"
