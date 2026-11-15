"""Bearer-token authentication (ADR-021, #57)."""

import hmac
import io
import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from core.app import create_app
from core.auth import MIN_TOKEN_LENGTH, check_authorization
from core.config import Settings
from core.events import EventRecorder
from core.identity import load_identity
from core.log import REDACTED, Redactor, configure_logging, secrets_from
from core.request_context import REQUEST_ID_HEADER
from router.contract import ProviderUnavailable
from router.routing import Router
from tests.test_chat import ANSWER, PROMPT, Fake, MemoryWriter, snapshot

TOKEN = "fake-api-token-auth-tests-0123456789abcdef"  # noqa: S105  # gitleaks:allow
WRONG = "fake-api-token-auth-WRONG-0123456789abcdef"  # gitleaks:allow
CHAT_BODY = {"messages": [{"role": "user", "content": PROMPT}]}


class App:
    def __init__(self, token: str | None = TOKEN, *, local_error: Exception | None = None) -> None:
        self.settings = Settings(
            _env_file=None, aether_api_token=SecretStr(token) if token is not None else None
        )
        self.log = io.StringIO()
        # Capture logging before create_app so startup warnings are seen.
        configure_logging(self.settings, stream=self.log)
        identity = load_identity()
        self.local = Fake("ollama", "local", error=local_error)
        self.writer = MemoryWriter(Redactor(secrets_from(self.settings)))
        self.app = create_app(
            self.settings,
            identity,
            router=Router([self.local], identity, probe=lambda: snapshot(4.0)),
            events=EventRecorder(self.writer),
        )
        configure_logging(self.settings, stream=self.log)  # create_app reset it to stdout
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.log.getvalue().splitlines() if x]


def _routes(routes: list[Any]) -> list[Route]:
    # FastAPI 0.162 wraps include_router() in an object holding ``original_router``.
    found: list[Route] = []
    for route in routes:
        if isinstance(route, Route):
            found.append(route)
        elif (inner := getattr(route, "original_router", None)) is not None:
            found.extend(_routes(inner.routes))
    return found


def protected_requests(app: FastAPI) -> list[tuple[str, str]]:
    """(method, path) for every registered route except /health, plus an unknown path."""
    found = []
    for route in _routes(app.routes):
        if route.path == "/health":
            continue
        for method in sorted(route.methods or {"GET"}):
            if method != "HEAD":
                found.append((method, route.path))
    return [*found, ("GET", "/no-such-route")]


def test_route_list_covers_chat_and_fastapi_docs() -> None:
    app = App().app
    paths = {path for _, path in protected_requests(app)}
    assert {"/chat", "/docs", "/redoc", "/openapi.json", "/no-such-route"} <= paths
    # Cross-check with the public schema: no documented route is missed.
    assert set(app.openapi()["paths"]) - {"/health"} <= paths
    assert "/health" not in paths


# --- every route except /health refuses without the right token ----------------------


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        f"Bearer {WRONG}",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {TOKEN}x",
        f"Basic {TOKEN}",
        TOKEN,
        "Bearer",
        "Bearer   ",
    ],
    ids=[
        "missing",
        "empty",
        "wrong",
        "prefix",
        "longer",
        "basic-scheme",
        "no-scheme",
        "scheme-only",
        "blank",
    ],
)
def test_every_protected_route_refuses_without_the_token(authorization: str | None) -> None:
    a = App()
    headers = {"authorization": authorization} if authorization is not None else {}

    for method, path in protected_requests(a.app):
        r = a.client.request(method, path, headers=headers, json=CHAT_BODY)
        assert r.status_code == 401, (method, path)
        assert r.headers["www-authenticate"] == "Bearer"
        assert r.json() == {
            "request_id": r.headers[REQUEST_ID_HEADER],
            "error": {
                "type": "Unauthorized",
                "message": "missing or invalid API token",
                "attempts": [],
            },
        }
        for secret in (TOKEN, WRONG):
            assert secret not in r.text
    assert a.local.requests == [] and a.writer.rows == []


def test_health_stays_open_without_a_token() -> None:
    assert App().client.get("/health").status_code == 200
    assert App(token=None).client.get("/health").status_code == 200


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_valid_token_is_served(scheme: str) -> None:
    a = App()

    r = a.client.post("/chat", json=CHAT_BODY, headers={"authorization": f"{scheme} {TOKEN}"})

    assert r.status_code == 200
    assert r.json()["text"] == ANSWER
    assert a.client.get("/openapi.json", headers={"authorization": f"Bearer {TOKEN}"}).is_success


def test_unauthenticated_large_body_is_refused_before_reading() -> None:
    a = App()
    r = a.client.post(
        "/chat",
        content=b"x" * (512 * 1024),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 401  # auth runs before the 413 body-size check


def test_websocket_refused_fail_closed() -> None:
    a = App()

    @a.app.websocket("/_test/ws")
    async def ws(websocket: Any) -> None:  # pragma: no cover - must never run
        await websocket.accept()

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        a.client.websocket_connect(
            "/_test/ws", headers={"authorization": f"Bearer {TOKEN}"}
        ) as conn,
    ):
        conn.receive_text()
    assert closed.value.code == 1008


# --- no token configured: refuse, never run open ------------------------------------


@pytest.mark.parametrize(
    ("token", "warning"),
    [
        (None, "API disabled: AETHER_API_TOKEN is not set; only /health is served"),
        ("", "API disabled: AETHER_API_TOKEN is not set; only /health is served"),
        ("   ", "API disabled: AETHER_API_TOKEN is not set; only /health is served"),
        (
            "short-but-not-empty-tok",
            f"API disabled: AETHER_API_TOKEN must be at least {MIN_TOKEN_LENGTH} "
            "characters with no surrounding spaces; only /health is served",
        ),
        (
            f" {TOKEN} ",
            f"API disabled: AETHER_API_TOKEN must be at least {MIN_TOKEN_LENGTH} "
            "characters with no surrounding spaces; only /health is served",
        ),
    ],
    ids=["unset", "empty", "blank", "too-short", "padded"],
)
def test_unconfigured_token_refuses_every_protected_route(
    token: str | None, warning: str, capsys: pytest.CaptureFixture[str]
) -> None:
    a = App(token=token)
    startup_out = capsys.readouterr().out  # create_app logs to stdout

    for authorization in (None, "Bearer anything", f"Bearer {token}" if token else None):
        headers = {"authorization": authorization} if authorization else {}
        for method, path in protected_requests(a.app):
            r = a.client.request(method, path, headers=headers, json=CHAT_BODY)
            assert r.status_code == 503, (method, path)
            assert r.json()["error"]["type"] == "AuthNotConfigured"
    assert a.client.get("/health").status_code == 200
    assert a.local.requests == []

    lines = [json.loads(x) for x in startup_out.splitlines() if x.startswith("{")]
    startup = [r for r in lines if r["message"].startswith("API disabled")]
    assert [r["message"] for r in startup] == [warning]
    assert startup[0]["level"] == "WARNING"
    if token and token.strip():
        assert token.strip() not in startup_out + a.log.getvalue()


# --- constant-time comparison -------------------------------------------------------


def test_comparison_uses_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    # core.auth looks up hmac.compare_digest at call time.
    monkeypatch.setattr(hmac, "compare_digest", spy)
    token = TOKEN.encode()

    assert check_authorization(b"Bearer " + token, token) is None
    assert check_authorization(b"Bearer " + WRONG.encode(), token) == "invalid"
    assert calls == [(token, token), (WRONG.encode(), token)]
    assert all(isinstance(x, bytes) for pair in calls for x in pair)


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "missing"),
        (b"", "malformed"),
        (b"Basic abc", "malformed"),
        (b"Bearer", "malformed"),
        (b"Bearer \xff\xfe", "invalid"),  # non-UTF-8 bytes: compared, not decoded
    ],
)
def test_check_authorization_reasons(header: bytes | None, reason: str) -> None:
    assert check_authorization(header, TOKEN.encode()) == reason


# --- the token never appears in logs, events or error bodies ------------------------


def test_failed_auth_log_has_reason_only() -> None:
    a = App()

    a.client.get("/chat")
    a.client.post("/chat", json=CHAT_BODY, headers={"authorization": "Basic x"})
    a.client.post("/chat", json=CHAT_BODY, headers={"authorization": f"Bearer {WRONG}"})

    failures = [r for r in a.records() if r["message"] == "Auth failed"]
    assert [(r["reason"], r["path"], r["level"]) for r in failures] == [
        ("missing", "/chat", "WARNING"),
        ("malformed", "/chat", "WARNING"),
        ("invalid", "/chat", "WARNING"),
    ]
    text = a.log.getvalue()
    assert WRONG not in text and TOKEN not in text
    assert "authorization" not in text.lower()


def test_token_redacted_in_log_message_extra_and_traceback() -> None:
    a = App()
    log = logging.getLogger("aether.test.auth")

    log.warning("token is %s", TOKEN, extra={"detail": f"header Bearer {TOKEN}"})
    try:
        raise RuntimeError(f"failed with {TOKEN}")
    except RuntimeError:
        log.exception("boom")

    text = a.log.getvalue()
    assert TOKEN not in text
    assert text.count(REDACTED) >= 3


def test_unconfigured_bearer_value_redacted_by_pattern() -> None:
    redact = Redactor()
    assert redact(f"Authorization: Bearer {WRONG}") == f"Authorization: Bearer {REDACTED}"
    assert redact("bearer abcdefgh12345") == f"bearer {REDACTED}"
    assert redact("Bearer short") == "Bearer short"  # under 8 chars: not a credential


@pytest.mark.parametrize(
    "prefix",
    ["BEARER ", "bearer ", "Bearer\t", "Bearer  ", "bEaReR \t "],
    ids=["upper", "lower", "tab", "two-spaces", "mixed"],
)
def test_bearer_pattern_any_casing_and_whitespace(prefix: str) -> None:
    # The word and the whitespace are kept exactly; only the credential is replaced.
    redact = Redactor()
    assert redact(f"Authorization: {prefix}{WRONG}") == f"Authorization: {prefix}{REDACTED}"
    assert WRONG not in redact(f"{prefix}{WRONG}")
    assert redact(f"{prefix}short") == f"{prefix}short"  # still 8+ characters only


def test_ordinary_text_with_the_word_bearer_is_not_corrupted() -> None:
    redact = Redactor()
    sentence = "The bearer of this note may collect the parcel; bearers of bad news too."
    assert redact(sentence) == sentence
    assert redact("Bearer") == "Bearer"
    assert redact("BEARER\n") == "BEARER\n"
    # Known trade-off (unchanged from before): a long word right after "bearer" is
    # indistinguishable from a credential, so it is redacted. That is the safe side.
    assert redact("a bearer instrument") == f"a bearer {REDACTED}"


def test_credential_ending_in_bearer_does_not_hide_the_next_one() -> None:
    # Review of #42: matches must not overlap, as with the old lookbehind.
    redact = Redactor()
    assert redact("Bearer abc.def.bearer SECRET12345") == f"Bearer {REDACTED} {REDACTED}"
    assert redact("Bearer abcdefgBEARER\tsecret999") == f"Bearer {REDACTED}\t{REDACTED}"


@pytest.mark.parametrize("prefix", ["bearer ", "BEARER ", "Bearer\t"])
def test_url_password_after_bearer_is_redacted(prefix: str) -> None:
    # Review of #42: URL passwords are redacted before the key patterns, so the
    # Bearer pattern can't eat the scheme and hide the URL.
    redact = Redactor()
    out = redact(f"{prefix}postgresql://user:longpassword99@host/db")
    assert "longpassword99" not in out
    assert f":{REDACTED}@host/db" in out


def test_token_in_provider_error_never_reaches_body_or_events() -> None:
    # A provider error message that (somehow) contains the token: scrubbed everywhere.
    a = App(local_error=ProviderUnavailable("ollama", f"rejected {TOKEN}"))

    r = a.client.post("/chat", json=CHAT_BODY, headers={"authorization": f"Bearer {TOKEN}"})

    assert r.status_code == 502
    assert TOKEN not in r.text
    assert r.json()["error"]["attempts"][0]["message"] == f"rejected {REDACTED}"
    assert a.writer.rows, "events were recorded"
    assert TOKEN not in repr(a.writer.rows)
    assert TOKEN not in a.log.getvalue()


def test_token_never_in_events_on_success() -> None:
    a = App()

    a.client.post("/chat", json=CHAT_BODY, headers={"authorization": f"Bearer {TOKEN}"})

    assert [e["event_type"] for e in a.writer.rows][-1] == "model.call.completed"
    assert TOKEN not in repr(a.writer.rows)


def test_token_not_in_settings_repr() -> None:
    settings = Settings(_env_file=None, aether_api_token=SecretStr(TOKEN))
    assert TOKEN not in repr(settings) and TOKEN not in str(settings.model_dump())
