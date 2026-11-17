"""Owner-only database admin step: set the app role's password (#1, #12).

Migration 0003 creates the app role (``aether_app``) with no password, so it
can't log in. This step sets the password that ``DATABASE_URL`` already
carries. It connects with the owner URL (``DATABASE_MIGRATION_URL``) and sends a
SCRAM-SHA-256 verifier, computed here, never the password itself, the same way
``psql``'s ``\\password`` does. So the plain password doesn't reach the server,
its statement log or the process list. Then it checks that Core can log in as
the app role, including the privileged-role check (``core.db``).

Run once after ``uv run alembic upgrade head``, and again after changing the
password in ``.env`` (fish)::

    uv run python -m core.db_admin set-app-password

Output names the role and the result only. Never a URL or a password.
"""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import sys
from urllib.parse import unquote, urlsplit

from sqlalchemy import text

from core.config import Settings, get_settings
from core.db import (
    DatabaseConfigError,
    DbStatus,
    check_database,
    classify,
    create_migration_engine,
    describe_target,
    url_status,
)
from core.log import Redactor, secrets_from

SCRAM_ITERATIONS = 4096  # Postgres's default (scram_iterations)
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
APP_ROLE = "aether_app"  # migrations/versions/0003_app_role.py
# The target must be the app role (or a member of it, as the live tests use) and
# hold no special attributes: never reset another service's or a superuser's
# password by mistake.
TARGET_QUERY = """
SELECT (r.rolname = :app OR pg_has_role(r.oid, :app, 'MEMBER'))
   AND NOT (r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
            OR r.rolbypassrls OR pg_has_role(r.oid, d.datdba, 'MEMBER'))
FROM pg_roles r, pg_database d
WHERE r.rolname = :r AND d.datname = current_database()
"""
USAGE = "usage: python -m core.db_admin set-app-password"


def scram_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS
) -> str:
    """A Postgres SCRAM-SHA-256 verifier (RFC 5802/7677) for ``password``.

    ASCII only: Postgres runs SASLprep on the password, which leaves printable
    ASCII unchanged; other text could be normalised differently than here.
    """
    if not password.isascii() or not password.isprintable():
        raise ValueError("the password must be printable ASCII (e.g. openssl rand -hex 24)")
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def set_password_sql(role: str, password: str) -> str:
    """``ALTER ROLE`` with a verifier. ``role`` must be a plain lower-case name."""
    if not _ROLE_NAME.fullmatch(role):
        raise ValueError("the role name must be lower-case letters, digits and _")
    # The verifier is base64, digits, '$' and ':' only: safe inside quotes.
    return f"ALTER ROLE \"{role}\" PASSWORD '{scram_verifier(password)}'"


def _app_credentials(settings: Settings) -> tuple[str, str]:
    status = url_status(settings)
    if status is not None:
        raise DatabaseConfigError(status.message)
    if settings.database_migration_url is None:
        raise DatabaseConfigError(
            "DATABASE_MIGRATION_URL is not set: this step connects as the owner role"
        )
    if settings.database_url is None:  # url_status already reports this
        raise DatabaseConfigError("DATABASE_URL is not set")
    parts = urlsplit(settings.database_url.get_secret_value())
    return parts.username or "", unquote(parts.password or "")


async def set_app_password(settings: Settings) -> DbStatus:
    """Set the app role's password from ``DATABASE_URL``, then check Core's login."""
    try:
        role, password = _app_credentials(settings)
        statement = set_password_sql(role, password)
    except (DatabaseConfigError, ValueError) as exc:
        return DbStatus(False, "misconfigured", str(exc))
    engine = create_migration_engine(settings)
    try:
        async with engine.begin() as conn:
            if not await conn.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": role}
            ):
                return DbStatus(
                    False,
                    "misconfigured",
                    f"role {role} does not exist: run `uv run alembic upgrade head` first",
                )
            if not await conn.scalar(text(TARGET_QUERY), {"r": role, "app": APP_ROLE}):
                return DbStatus(
                    False,
                    "misconfigured",
                    f"DATABASE_URL must use the app role {APP_ROLE}; role {role} is "
                    "not it (or is privileged), so its password was not changed",
                )
            # Keep even the verifier out of the server log (the owner is a superuser).
            await conn.exec_driver_sql("SET LOCAL log_statement = 'none'")
            # Not text(): it would read ":<base64>" in the verifier as a bind parameter.
            await conn.exec_driver_sql(statement)
    except Exception as exc:
        return classify(exc, "the owner role (DATABASE_MIGRATION_URL)")
    finally:
        await engine.dispose()
    status = await check_database(settings)
    if not status.ok:
        return status
    return DbStatus(True, "ok", f"password set; Core can log in as {describe_target(settings)}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["set-app-password"]:
        print(USAGE, file=sys.stderr)
        return 2
    settings = get_settings()
    status = asyncio.run(set_app_password(settings))
    # Messages are built from safe parts; redact anyway as a second layer.
    print(Redactor(secrets_from(settings))(f"{status.problem}: {status.message}"))
    return 0 if status.ok else 1


if __name__ == "__main__":
    sys.exit(main())
