"""The leftover test-role sweep (#61). Live: runs only with AETHER_DB_TESTS=1."""

import asyncio
import secrets

from core.config import Settings
from core.db_admin import scram_verifier
from tests.conftest import (
    LIVE_DB,
    TEST_ROLE_PREFIX,
    _as_owner,
    _sweep_leftover_test_roles,
)

pytestmark = LIVE_DB


def _role_exists(owner: Settings, name: str) -> bool:
    from sqlalchemy import text

    from core.db import create_migration_engine

    async def run() -> bool:
        engine = create_migration_engine(owner)
        try:
            async with engine.connect() as conn:
                found = await conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}
                )
                return found.first() is not None
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_leftover_test_role_is_dropped_and_others_are_kept(live_owner: Settings) -> None:
    leftover = f"{TEST_ROLE_PREFIX}{secrets.token_hex(6)}"
    lookalike = f"{TEST_ROLE_PREFIX}keep_{secrets.token_hex(3)}"  # not the fixture's shape
    verifier = scram_verifier(secrets.token_hex(24))
    for name in (leftover, lookalike):
        asyncio.run(_as_owner(live_owner, f"CREATE ROLE {name} LOGIN PASSWORD '{verifier}'"))
    try:
        dropped = asyncio.run(_sweep_leftover_test_roles(live_owner))
        assert leftover in dropped
        assert not _role_exists(live_owner, leftover)
        assert _role_exists(live_owner, lookalike)
    finally:
        for name in (leftover, lookalike):
            asyncio.run(_as_owner(live_owner, f"DROP ROLE IF EXISTS {name}"))


def test_the_running_tests_own_role_is_not_dropped(live_app: Settings) -> None:
    # live_app's role has an open session while the app engine is connected.
    from urllib.parse import urlsplit

    from sqlalchemy import text

    from core.db import create_engine

    assert live_app.database_url is not None
    role = urlsplit(live_app.database_url.get_secret_value()).username
    owner = Settings(_env_file=None, database_url=live_app.database_migration_url)

    async def run() -> list[str]:
        engine = create_engine(live_app)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                return await _sweep_leftover_test_roles(owner)
        finally:
            await engine.dispose()

    assert role not in asyncio.run(run())
    assert role is not None and _role_exists(owner, role)
