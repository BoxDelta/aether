"""core.db_admin without a database: the SCRAM verifier and the CLI's guards (#1, #12).

The live path (setting the password and logging in with it) is in
tests/test_db_roles_live.py.
"""

import asyncio
import base64
import hashlib
import hmac
import re

import pytest

import core.db_admin
from core.config import Settings
from core.db_admin import scram_verifier, set_app_password, set_password_sql

APP_PASSWORD = "fake-app-pass-93b1e6f0d2a4"  # noqa: S105  # gitleaks:allow
OWNER_URL = "postgresql+asyncpg://aether:fake-owner-pass-0c4d8e2a17f5@127.0.0.1:5432/aether"
APP_URL = f"postgresql+asyncpg://aether_app:{APP_PASSWORD}@127.0.0.1:5432/aether"
VERIFIER = re.compile(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_verifier_format_and_random_salt() -> None:
    first, second = scram_verifier(APP_PASSWORD), scram_verifier(APP_PASSWORD)
    assert VERIFIER.fullmatch(first)
    assert first != second  # a new salt each time
    assert APP_PASSWORD not in first


def test_verifier_matches_rfc_5802_derivation() -> None:
    # Independent derivation (RFC 5802 §3) with the RFC 7677 example inputs.
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = scram_verifier("pencil", salt=salt)
    salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
    stored = hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    assert verifier == (
        "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ=="
        f"${base64.b64encode(stored).decode()}:{base64.b64encode(server).decode()}"
    )


def test_rfc_7677_server_signature() -> None:
    # The RFC's own test vector: the ServerKey from our verifier signs the RFC's
    # AuthMessage to the server signature published in RFC 7677 §3.
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    server_key = base64.b64decode(scram_verifier("pencil", salt=salt).rsplit(":", 1)[1])
    auth_message = (
        "n=user,r=rOprNGfwEbeRWgbNEkqO,"
        "r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
        "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    )
    signature = hmac.new(server_key, auth_message.encode(), "sha256").digest()
    assert base64.b64encode(signature).decode() == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="


@pytest.mark.parametrize("password", ["pässwört-long", "tab\there-long", "new\nline-long"])
def test_verifier_refuses_non_printable_ascii(password: str) -> None:
    with pytest.raises(ValueError, match="printable ASCII") as info:
        scram_verifier(password)
    assert password not in str(info.value)


def test_set_password_sql_has_no_password_and_a_safe_role() -> None:
    sql = set_password_sql("aether_app", APP_PASSWORD)
    assert sql.startswith('ALTER ROLE "aether_app" PASSWORD \'SCRAM-SHA-256$4096:')
    assert APP_PASSWORD not in sql
    for bad in ('aether"; DROP TABLE events; --', "Aether", "", "a" * 64):
        with pytest.raises(ValueError):
            set_password_sql(bad, APP_PASSWORD)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({}, "DATABASE_URL is not set"),
        ({"database_url": APP_URL}, "DATABASE_MIGRATION_URL is not set"),
        ({"database_url": OWNER_URL, "database_migration_url": OWNER_URL}, "same role"),
        (
            {
                "database_url": APP_URL.replace(APP_PASSWORD, "short"),
                "database_migration_url": OWNER_URL,
            },
            "at least 8",
        ),
    ],
)
def test_set_app_password_refuses_bad_settings_without_connecting(
    kwargs: dict[str, str], error: str
) -> None:
    status = asyncio.run(set_app_password(settings(**kwargs)))
    assert status.problem in {"misconfigured", "not_configured"}
    assert error in status.message
    assert "://" not in status.message and "fake-" not in status.message


def test_cli_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert core.db_admin.main([]) == 2
    assert core.db_admin.main(["drop-everything"]) == 2
    assert "usage" in capsys.readouterr().err
