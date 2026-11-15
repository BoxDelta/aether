"""Postgres setup (#20) without a database: settings, redaction, error mapping, Alembic wiring."""

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

import core.config
import core.db
from core.config import Settings
from core.db import DbStatus, check_database, classify, create_engine, describe_target
from core.log import REDACTED, Redactor, configure_logging, secrets_from

# Fake credentials for tests only; never a real password.
DB_PASSWORD = "fake-db-pass-7f3a9c2e41b8"  # noqa: S105  # gitleaks:allow
DB_URL = f"postgresql+asyncpg://aether:{DB_PASSWORD}@127.0.0.1:5432/aether"
TARGET = "aether@127.0.0.1:5432/aether"
REPO = Path(__file__).resolve().parent.parent

log = logging.getLogger("aether.test.db")


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


# --- settings ---------------------------------------------------------------------


def test_database_url_is_optional_and_empty_means_unset() -> None:
    assert settings().database_url is None
    assert settings(database_url="  ").database_url is None


def test_database_url_is_secret() -> None:
    s = settings(database_url=DB_URL)
    assert s.database_url is not None
    assert s.database_url.get_secret_value() == DB_URL
    assert DB_PASSWORD not in repr(s) and DB_PASSWORD not in str(s.model_dump())


BAD_URLS = [
    (f"postgresql://aether:{DB_PASSWORD}@127.0.0.1/aether", "must start with"),
    (f"mysql+aiomysql://aether:{DB_PASSWORD}@127.0.0.1/aether", "must start with"),
    (f"postgresql+asyncpg://aether:{DB_PASSWORD}@127.0.0.1/", "host and a database"),
    (f"postgresql+asyncpg://aether:{DB_PASSWORD}@/aether", "host and a database"),
    (f"postgresql+asyncpg://aether:{DB_PASSWORD}@[::1/aether", "not a valid URL"),
    (f"postgresql+asyncpg://aether:{DB_PASSWORD}@h:99999/aether", "not a valid URL"),
]


@pytest.mark.parametrize(("url", "error"), BAD_URLS)
def test_bad_database_url_never_raises_from_settings(url: str, error: str) -> None:
    # A pydantic ValidationError would carry the raw URL in .errors()/.json().
    s = settings(database_url=url)
    assert s.database_url is not None


@pytest.mark.parametrize(("url", "error"), BAD_URLS)
def test_bad_database_url_reported_without_echoing_it(url: str, error: str) -> None:
    s = settings(database_url=url)

    status = asyncio.run(check_database(s))
    assert status.problem == "misconfigured"
    assert error in status.message
    assert DB_PASSWORD not in status.message

    with pytest.raises(core.db.DatabaseConfigError) as info:
        create_engine(s)
    assert error in str(info.value)
    assert DB_PASSWORD not in str(info.value)
    assert describe_target(s) == "(invalid DATABASE_URL)"


def test_secrets_from_includes_db_password_raw_and_decoded() -> None:
    url = "postgresql+asyncpg://aether:pa%40ss-word-123@127.0.0.1/aether"
    found = secrets_from(settings(database_url=url))
    assert "pa%40ss-word-123" in found
    assert "pa@ss-word-123" in found


def test_secrets_from_without_db() -> None:
    assert secrets_from(settings()) == []


# --- redaction (issue acceptance) -------------------------------------------------


def test_configured_db_password_never_in_logs() -> None:
    out = io.StringIO()
    configure_logging(settings(database_url=DB_URL), stream=out)

    log.info("pw %s", DB_PASSWORD, extra={"dsn": f"password={DB_PASSWORD} host=x"})
    log.warning("bare %s", DB_PASSWORD, extra={"nested": {"pw": DB_PASSWORD}})
    try:
        raise ConnectionError(f"login with {DB_PASSWORD} failed")
    except ConnectionError:
        log.exception("connect failed")

    text = out.getvalue()
    assert DB_PASSWORD not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert records[0]["message"] == f"pw {REDACTED}"
    assert records[0]["dsn"] == f"password={REDACTED} host=x"
    assert REDACTED in records[1]["nested"]
    assert f"login with {REDACTED} failed" in records[2]["exc_info"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgresql+asyncpg://aether:hunter22@127.0.0.1:5432/aether",
            f"postgresql+asyncpg://aether:{REDACTED}@127.0.0.1:5432/aether",
        ),
        ("postgres://u:p@db.local/x", f"postgres://u:{REDACTED}@db.local/x"),
        ("redis://:only-pw@cache:6379/0", f"redis://:{REDACTED}@cache:6379/0"),
        ("https://bob:t0k/en:x@example.org/p", f"https://bob:{REDACTED}@example.org/p"),
        ("amqp://guest:guest@rabbit", f"amqp://guest:{REDACTED}@rabbit"),
        (
            "two: a://u1:p1@h1 and b://u2:p2@h2.",
            f"two: a://u1:{REDACTED}@h1 and b://u2:{REDACTED}@h2.",
        ),
        ('{"dsn": "postgresql://u:p@h/db"}', f'{{"dsn": "postgresql://u:{REDACTED}@h/db"}}'),
    ],
)
def test_url_credentials_redacted_even_when_not_configured(raw: str, expected: str) -> None:
    assert Redactor()(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "http://127.0.0.1:11434/api/chat",
        "https://user@example.org/path",
        "ssh git@github.com:BoxDelta/aether.git",
        "time 12:30 and mail me@example.org",
    ],
)
def test_urls_without_password_untouched(text: str) -> None:
    assert Redactor()(text) == text


def test_url_redaction_in_logs_keeps_user_and_host() -> None:
    out = io.StringIO()
    configure_logging(settings(), stream=out)  # nothing configured

    log.error("cannot connect to %s", "postgresql://aether:unconfigured-pw@db:5432/aether")

    record = json.loads(out.getvalue())
    assert record["message"] == f"cannot connect to postgresql://aether:{REDACTED}@db:5432/aether"


# --- engine and connection check ---------------------------------------------------


def test_create_engine_requires_url() -> None:
    with pytest.raises(core.db.DatabaseNotConfigured):
        create_engine(settings())


def test_engine_repr_hides_password() -> None:
    engine = create_engine(settings(database_url=DB_URL))
    try:
        assert DB_PASSWORD not in repr(engine) and DB_PASSWORD not in str(engine.url)
    finally:
        asyncio.run(engine.dispose())


def test_describe_target_has_no_password() -> None:
    assert describe_target(settings(database_url=DB_URL)) == TARGET
    assert describe_target(settings()) == "(not configured)"


class InvalidPasswordError(Exception):  # same name as asyncpg's
    pass


class InvalidCatalogNameError(Exception):
    pass


class WrappedDBAPIError(Exception):
    """Shaped like sqlalchemy.exc.DBAPIError: the driver error is in ``orig``."""

    def __init__(self, orig: BaseException) -> None:
        super().__init__(f"(wrapped) {orig}")
        self.orig = orig


def raised_from(outer: Exception, inner: BaseException) -> Exception:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise outer from exc
    except Exception as exc:
        return exc


@pytest.mark.parametrize(
    ("exc", "problem"),
    [
        (WrappedDBAPIError(InvalidPasswordError(f"pw {DB_PASSWORD} rejected")), "auth_failed"),
        (raised_from(RuntimeError("x"), InvalidPasswordError("no")), "auth_failed"),
        (WrappedDBAPIError(InvalidCatalogNameError("no db")), "database_missing"),
        (WrappedDBAPIError(ConnectionRefusedError(111, "refused")), "unreachable"),
        (raised_from(RuntimeError("x"), OSError("Name or service not known")), "unreachable"),
        (TimeoutError(), "timeout"),
        (WrappedDBAPIError(ValueError(f"weird {DB_PASSWORD}")), "error"),
    ],
)
def test_classify(exc: Exception, problem: str) -> None:
    status = classify(exc, TARGET)
    assert status.problem == problem
    assert not status.ok
    assert TARGET in status.message
    assert DB_PASSWORD not in status.message


def test_auth_failed_message_explains_the_volume_trap() -> None:
    message = classify(WrappedDBAPIError(InvalidPasswordError()), TARGET).message
    assert "POSTGRES_PASSWORD" in message
    assert "first created" in message


def test_check_database_not_configured() -> None:
    status = asyncio.run(check_database(settings()))
    assert status == DbStatus(False, "not_configured", "DATABASE_URL is not set (see .env.example)")


def test_check_database_unreachable_real_driver() -> None:
    # Port 1 on loopback: nothing listens, so asyncpg gets a real connection refusal.
    url = f"postgresql+asyncpg://aether:{DB_PASSWORD}@127.0.0.1:1/aether"
    status = asyncio.run(check_database(settings(database_url=url)))
    assert status.problem == "unreachable"
    assert "aether@127.0.0.1:1/aether" in status.message
    assert DB_PASSWORD not in status.message


def test_cli_prints_status_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"postgresql+asyncpg://aether:{DB_PASSWORD}@127.0.0.1:1/aether"
    monkeypatch.setattr(core.db, "get_settings", lambda: settings(database_url=url))

    assert core.db.main() == 1
    out = capsys.readouterr().out
    assert out.startswith("unreachable: cannot reach Postgres at aether@127.0.0.1:1/aether")
    assert DB_PASSWORD not in out


# --- Alembic wiring (offline, no database) -----------------------------------------


def test_alembic_offline_upgrade_uses_settings_and_hides_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # env.py reads core.config.get_settings at run time; never the developer's .env here.
    monkeypatch.setattr(core.config, "get_settings", lambda: settings(database_url=DB_URL))
    config = Config(str(REPO / "alembic.ini"))

    command.upgrade(config, "head", sql=True)

    out = capsys.readouterr().out
    assert "CREATE TABLE alembic_version" in out
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001')" in out
    assert DB_PASSWORD not in out


def test_alembic_has_one_head() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    assert script.get_heads() == ["0002"]


def test_alembic_ini_has_no_url() -> None:
    text = (REPO / "alembic.ini").read_text(encoding="utf-8")
    assert "sqlalchemy.url" not in text
    assert "://" not in text


def _compose() -> dict[str, Any]:
    import yaml

    loaded: dict[str, Any] = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    return loaded


def test_compose_binds_loopback_and_takes_password_from_env() -> None:
    pg = _compose()["services"]["postgres"]
    assert pg["ports"] == ["127.0.0.1:5432:5432"]
    assert pg["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert pg["volumes"] == ["pgdata:/var/lib/postgresql"]


# --- shared helpers (#34) -----------------------------------------------------------


def test_url_status_categories() -> None:
    assert core.db.url_status(settings()) == DbStatus(
        False, "not_configured", core.db.NOT_SET_MESSAGE
    )
    assert core.db.NOT_SET_MESSAGE == "DATABASE_URL is not set (see .env.example)"
    assert core.db.url_status(settings(database_url=DB_URL)) is None


@pytest.mark.parametrize(("url", "error"), BAD_URLS)
def test_url_status_never_echoes_the_url(url: str, error: str) -> None:
    status = core.db.url_status(settings(database_url=url))
    assert status is not None
    assert status.problem == "misconfigured"
    assert error in status.message
    assert DB_PASSWORD not in status.message
    assert "aether:" not in status.message  # no part of the URL at all


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


def test_engine_for_disposes_only_its_own_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Any] = []

    def fake_create(_: Settings) -> _FakeEngine:
        engine = _FakeEngine()
        made.append(engine)
        return engine

    monkeypatch.setattr(core.db, "create_engine", fake_create)
    passed: Any = _FakeEngine()

    async def run() -> None:
        async with core.db.engine_for(settings(), passed) as e:
            assert e is passed
        async with core.db.engine_for(settings(), None) as e:
            assert e is made[0]
        with pytest.raises(RuntimeError):
            async with core.db.engine_for(settings(), None):
                raise RuntimeError("boom")

    async def returns_early() -> str:
        # The path check_database and ready._database use: return from inside.
        async with core.db.engine_for(settings(), None):
            return "early"

    asyncio.run(run())
    assert asyncio.run(returns_early()) == "early"
    assert passed.disposed == 0
    assert [m.disposed for m in made] == [1, 1, 1]
