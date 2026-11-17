"""Fake secrets for the secret-format tests, built at runtime (#70).

gitleaks scans the repository, so no secret-shaped literal is committed: every
value here is joined from pieces that look like nothing on their own. Each ``core``
is the part a check must never echo; ``text`` wraps it in ordinary words.
"""

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class FakeSecret:
    family: str
    core: str  # the secret-shaped part
    text: str  # the core inside ordinary content


def _wrap(core: str) -> str:
    return f"canary-fakesecret-7e3a remember this: {core} (invented for a test)"


def anthropic_key() -> str:
    return "sk-" + "ant-" + "api03-" + "fake" + secrets.token_hex(10)


def bearer(word: str = "Bearer", space: str = " ") -> str:
    return word + space + "fake" + "tok" + secrets.token_hex(8)


def url_with_password() -> str:
    password = "fake" + "pw" + secrets.token_hex(6)
    return "postgresql://" + "alice" + ":" + password + "@" + "db.invalid:5432/aether"


def configured_value() -> str:
    """A random value to configure as a secret (e.g. an API token)."""
    return "fakecfg" + secrets.token_hex(12)


def by_family(configured: str) -> list[FakeSecret]:
    """One fake per family the redactor knows, plus the configured value."""
    cores = {
        "anthropic key": anthropic_key(),
        "Bearer credential": bearer(),
        "bearer, lower case, tab": bearer("bearer", "\t"),
        "BEARER, upper case, several spaces": bearer("BEARER", "   "),
        "URL with a password": url_with_password(),
        "configured value": configured,
    }
    return [FakeSecret(family, core, _wrap(core)) for family, core in cores.items()]


# Ordinary text a check must accept: no format matches and nothing configured appears.
NORMAL_TEXTS = (
    "canary-fakesecret-7e3a the owner prefers tea in the morning",
    "The project uses the bearer of good news as a metaphor.",
    "See https://example.invalid/docs for the plan; ask alice@example.invalid.",
    "Ticket sk-1234 is about the skiing trip.",
    "A key-value store is fine here: key=preferred_name.",
)
