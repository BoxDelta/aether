"""The leftover test-role sweep (#61). Live: runs only with AETHER_DB_TESTS=1."""

import asyncio
import secrets
import time

from core.config import Settings
from core.db_admin import scram_verifier
from tests.conftest import (
    LIVE_DB,
    SWEEP_MIN_AGE_S,
    TEST_ROLE_PREFIX,
    _as_owner,
    _sweep_leftover_test_roles,
    role_comment,
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
    old = f"{TEST_ROLE_PREFIX}{secrets.token_hex(6)}"
    unnoted = f"{TEST_ROLE_PREFIX}{secrets.token_hex(6)}"  # made before the note existed
    recent = f"{TEST_ROLE_PREFIX}{secrets.token_hex(6)}"  # another run, going on now
    lookalike = f"{TEST_ROLE_PREFIX}keep_{secrets.token_hex(3)}"  # not the fixture's shape
    names = (old, unnoted, recent, lookalike)
    verifier = scram_verifier(secrets.token_hex(24))
    for name in names:
        asyncio.run(_as_owner(live_owner, f"CREATE ROLE {name} LOGIN PASSWORD '{verifier}'"))
    long_ago = role_comment(time.time() - SWEEP_MIN_AGE_S - 60)
    asyncio.run(_as_owner(live_owner, f"COMMENT ON ROLE {old} IS '{long_ago}'"))
    asyncio.run(_as_owner(live_owner, f"COMMENT ON ROLE {recent} IS '{role_comment()}'"))
    try:
        dropped = asyncio.run(_sweep_leftover_test_roles(live_owner))
        assert old in dropped and unnoted in dropped
        assert not _role_exists(live_owner, old)
        assert _role_exists(live_owner, recent)
        assert _role_exists(live_owner, lookalike)
    finally:
        for name in names:
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
