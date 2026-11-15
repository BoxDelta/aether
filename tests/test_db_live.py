"""Live checks against the local Postgres from docker-compose.yml (#20).

Opt-in: these read DATABASE_URL from .env and connect to the real container.
They only read (SELECT 1, the alembic_version row) and try one login with a
wrong password; they never create, change or drop anything.

Run (fish):  env AETHER_DB_TESTS=1 uv run pytest -m db
"""

import asyncio
import io
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr
from sqlalchemy import text

from core.config import Settings
from core.db import check_database, create_engine
from core.log import configure_logging
from tests.conftest import LIVE_DB

pytestmark = LIVE_DB

WRONG_PASSWORD = "definitely-wrong-password-for-test"  # noqa: S105  # gitleaks:allow


def real_password(s: Settings) -> str:
    assert s.database_url is not None
    return urlsplit(s.database_url.get_secret_value()).password or ""


def with_wrong_password(s: Settings) -> Settings:
    assert s.database_url is not None
    parts = urlsplit(s.database_url.get_secret_value())
    netloc = f"{parts.username}:{WRONG_PASSWORD}@{parts.hostname}:{parts.port or 5432}"
    return s.model_copy(
        update={"database_url": SecretStr(urlunsplit(parts._replace(netloc=netloc)))}
    )


def test_live_database_reachable(live: Settings) -> None:
    status = asyncio.run(check_database(live))
    assert status.ok, status.message
    assert status.problem == "ok"


def test_live_wrong_password_reported_safely(live: Settings) -> None:
    status = asyncio.run(check_database(with_wrong_password(live)))
    assert status.problem == "auth_failed"
    assert WRONG_PASSWORD not in status.message
    assert real_password(live) not in status.message


def test_live_driver_error_redacted_in_logs(live: Settings) -> None:
    out = io.StringIO()
    wrong = with_wrong_password(live)
    configure_logging(wrong, stream=out)

    async def connect() -> None:
        engine = create_engine(wrong)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(connect())
    except Exception:
        logging.getLogger("aether.test.db").exception("connect failed")

    logged = out.getvalue()
    assert "exc_info" in logged  # the real asyncpg traceback was logged...
    assert WRONG_PASSWORD not in logged  # ...without the password in it
    assert real_password(live) not in logged


def test_live_migrations_at_head(live: Settings) -> None:
    async def version() -> str | None:
        engine = create_engine(live)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = Path(__file__).resolve().parent.parent
    head = ScriptDirectory.from_config(Config(str(repo / "alembic.ini"))).get_current_head()
    assert asyncio.run(version()) == head
