"""The shared "does this look like a secret?" check (core/log.py ``SecretCheck``, #70).

The memory store refuses with it today and cloud sends (#8) will reuse it, so it
must stay the Redactor's own patterns and configured values, never a copy.
Fake secrets are built at runtime (tests/fake_secrets.py): gitleaks scans the repo.
"""

import inspect
from pathlib import Path

import pytest

from core import log
from core.config import Settings
from core.log import MIN_SECRET_LENGTH, SECRET_FORMATS, Redactor, SecretCheck, secrets_from
from tests.fake_secrets import NORMAL_TEXTS, FakeSecret, by_family, configured_value

ROOT = Path(__file__).resolve().parent.parent
CONFIGURED = configured_value()
FAKES = by_family(CONFIGURED)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("fake", FAKES, ids=lambda f: f.family)
def test_every_family_is_found(fake: FakeSecret) -> None:
    assert SecretCheck([CONFIGURED])(fake.text)


@pytest.mark.parametrize("text", NORMAL_TEXTS)
def test_normal_text_is_not_found(text: str) -> None:
    assert not SecretCheck([CONFIGURED])(text)


def test_configured_values_come_from_settings() -> None:
    token = configured_value()
    password = "fakedbpw" + configured_value()[-10:]
    check = SecretCheck.from_settings(
        _settings(
            aether_api_token=token,
            database_url="postgresql+psycopg://aether_app:" + password + "@db.invalid/aether",
        )
    )
    assert check(f"the token is {token}")
    assert check(f"the password is {password}")  # by value, without any URL around it
    assert not SecretCheck.from_settings(_settings())(f"the token is {token}")


def test_short_configured_values_do_not_match_everything() -> None:
    check = SecretCheck(["a", "the", "x" * (MIN_SECRET_LENGTH - 1)])
    for text in (*NORMAL_TEXTS, "x" * 50):
        assert not check(text)


def test_repr_holds_no_value() -> None:
    value = configured_value()
    assert value not in repr(SecretCheck([value]))


# --- drift: one definition of the patterns and values --------------------------------


def test_the_check_and_the_redactor_share_one_pattern_list() -> None:
    # The very same compiled objects, in the redactor's order (URLs first).
    redactor_patterns = (log._URL_CREDENTIALS, *log._KEY_PATTERNS)
    assert len(SECRET_FORMATS) == len(redactor_patterns)
    assert all(a is b for a, b in zip(SECRET_FORMATS, redactor_patterns, strict=True))
    # Both classes read SECRET_FORMATS and the shared helper for values, nothing else.
    for cls in (Redactor, SecretCheck):
        source = inspect.getsource(cls)
        assert "SECRET_FORMATS" in source and "_usable_secrets(" in source
        assert "re.compile" not in source and "_KEY_PATTERNS" not in source
        assert "_URL_CREDENTIALS" not in source


@pytest.mark.parametrize(
    "text",
    [
        *(f.text for f in FAKES),
        *NORMAL_TEXTS,
        "bearer postgresql://u:" + "p" * 9 + "@h/db",
        "Bearer short",
        "",
    ],
)
def test_found_exactly_when_the_redactor_would_change_the_text(text: str) -> None:
    values = [CONFIGURED, "tiny"]
    assert SecretCheck(values)(text) == (Redactor(values)(text) != text)


def test_the_store_uses_this_check_and_no_patterns_of_its_own() -> None:
    from memory.store import MemoryStore

    params = MemoryStore.__init__.__annotations__
    assert params["secret_check"] is SecretCheck
    for path in sorted((ROOT / "memory").rglob("*.py")):
        source = path.read_text()
        for fragment in ("sk-ant", "bearer", "://"):
            assert fragment not in source.lower(), f"{path.name} has its own {fragment!r}"


def test_secrets_from_is_the_only_value_source() -> None:
    # SecretCheck.from_settings is secrets_from(settings), so the values the log
    # redacts and the values memory refuses can't differ.
    token = configured_value()
    settings = _settings(aether_api_token=token)
    assert token in secrets_from(settings)
    assert SecretCheck.from_settings(settings)(token)
