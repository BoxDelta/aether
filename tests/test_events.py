"""Events (#21) without a database: what gets stored, and what never does."""

import re
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

import core.config
from core.config import Settings
from core.events import (
    MAX_TEXT,
    REMOVED_ECHO,
    Event,
    call_completed,
    events_table,
    provider_unavailable,
    request_failed,
    request_received,
    request_texts,
    route_selected,
    to_row,
)
from core.log import REDACTED, Redactor
from router.contract import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderTimeout,
    ProviderUnavailable,
    Usage,
)
from router.routing import Attempt, NoRoute, RouteDecision, RouteFailed, RouteOptions

# Fakes for tests only.
FAKE_SECRET = "fake-configured-secret-9d2c71ab"  # noqa: S105  # gitleaks:allow
FAKE_KEY = "sk-ant-api03-FAKE-unconfigured-0123456789"  # gitleaks:allow
PROMPT = "PRIVATE-PROMPT-MARKER: my bank PIN is 4711"
SYSTEM = "PRIVATE-SYSTEM-MARKER task instructions"
ANSWER = "PRIVATE-ANSWER-MARKER"
REPO = Path(__file__).resolve().parent.parent

REDACT = Redactor([FAKE_SECRET])
OPTIONS = RouteOptions(allow_cloud=True)


@pytest.fixture(autouse=True)
def ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.events.get_request_id", lambda: "req-123")
    monkeypatch.setattr("core.events.get_session_id", lambda: "sess-456")


def make_request() -> ModelRequest:
    return ModelRequest(
        messages=(Message(role="user", content=PROMPT),), system=SYSTEM, privacy="personal"
    )


def error_with_secret_cause() -> ProviderUnavailable:
    """A provider error whose __cause__ carries a key, a secret and the prompt."""
    try:
        try:
            raise RuntimeError(f"upstream said {FAKE_KEY} {FAKE_SECRET} for {PROMPT}")
        except RuntimeError as inner:
            raise ProviderUnavailable("claude", "cannot connect to the Claude API") from inner
    except ProviderUnavailable as exc:
        return exc


def stored(row: dict[str, Any]) -> str:
    return repr(row)


def assert_clean(text: str) -> None:
    for forbidden in (FAKE_SECRET, FAKE_KEY, PROMPT, SYSTEM, ANSWER, "Traceback", "upstream said"):
        assert forbidden not in text, forbidden


# --- builders store metadata only ----------------------------------------------------


def test_request_received_has_no_content() -> None:
    event = request_received(make_request(), OPTIONS)
    row = to_row(event, REDACT)
    assert row["event_type"] == "request.received"
    assert row["privacy"] == "personal"
    assert row["allow_cloud"] is True
    assert (row["request_id"], row["session_id"]) == ("req-123", "sess-456")
    assert_clean(stored(row))


def test_route_selected_stores_decision_and_reason() -> None:
    decision = RouteDecision("ollama", "local", "local preferred: free VRAM 4096 MiB")
    row = to_row(route_selected(decision, make_request(), OPTIONS), REDACT)
    assert (row["provider"], row["provider_kind"]) == ("ollama", "local")
    assert row["route_reason"] == "local preferred: free VRAM 4096 MiB"


def test_call_completed_stores_latency_and_tokens_not_answer() -> None:
    response = ModelResponse(
        text=ANSWER,
        provider="claude",
        model="claude-opus-5-5",
        kind="cloud",
        usage=Usage(input_tokens=12, output_tokens=34),
        latency_ms=812.5,
    )
    row = to_row(call_completed(response, make_request()), REDACT)
    assert row["outcome"] == "ok"
    assert (row["latency_ms"], row["input_tokens"], row["output_tokens"]) == (812.5, 12, 34)
    assert row["model"] == "claude-opus-5-5"
    assert_clean(stored(row))


def test_refusal_outcome() -> None:
    response = ModelResponse(
        text="", provider="claude", model="m", kind="cloud", finish_reason="refusal"
    )
    assert call_completed(response, make_request()).outcome == "refusal"


# --- issue acceptance: error with a secret-carrying __cause__ ------------------------


def test_provider_error_stores_only_type_and_message() -> None:
    exc = error_with_secret_cause()
    assert FAKE_KEY in str(exc.__cause__)  # the cause really carries the key

    row = to_row(provider_unavailable(Attempt.from_error(exc), make_request()), REDACT)

    assert row["error_type"] == "ProviderUnavailable"
    assert row["error_message"] == "cannot connect to the Claude API"
    assert_clean(stored(row))


def test_route_failed_stores_attempts_without_causes() -> None:
    first = error_with_secret_cause()
    second = ProviderTimeout("ollama", "Ollama did not respond in time")
    failed = RouteFailed(
        "fallback ollama also failed (ProviderTimeout)",
        [Attempt.from_error(first), Attempt.from_error(second)],
    )

    row = to_row(request_failed(failed, make_request(), OPTIONS), REDACT)

    assert row["error_type"] == "RouteFailed"
    assert row["attempts"] == [
        {
            "provider": "claude",
            "error_type": "ProviderUnavailable",
            "message": "cannot connect to the Claude API",
            "retryable": True,
        },
        {
            "provider": "ollama",
            "error_type": "ProviderTimeout",
            "message": "Ollama did not respond in time",
            "retryable": True,
        },
    ]
    assert_clean(stored(row))


def test_no_route_event() -> None:
    row = to_row(
        request_failed(NoRoute("secret data is never sent to any model"), make_request(), OPTIONS),
        REDACT,
    )
    assert row["error_type"] == "NoRoute"
    assert row["attempts"] is None


# --- write-time scrubbing (backstops) ------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (f"bad key {FAKE_SECRET}", f"bad key {REDACTED}"),  # configured secret
        (f"bad key {FAKE_KEY}", f"bad key {REDACTED}"),  # key-shaped, unconfigured
        ("dsn postgresql://u:hunter22@db/x", f"dsn postgresql://u:{REDACTED}@db/x"),
    ],
)
def test_secrets_in_messages_are_redacted(message: str, expected: str) -> None:
    attempt = Attempt("claude", "ProviderBadResponse", message, False)
    event = provider_unavailable(attempt, make_request())
    failed = request_failed(RouteFailed(message, [attempt]), make_request(), OPTIONS)

    assert to_row(event, REDACT)["error_message"] == expected
    row = to_row(failed, REDACT)
    assert row["route_reason"] == expected
    assert row["attempts"][0]["message"] == expected


def test_prompt_echo_is_removed_when_request_given() -> None:
    request = make_request()
    attempt = Attempt("ollama", "ProviderBadResponse", f"model said: {PROMPT}", False)

    row = to_row(provider_unavailable(attempt, request), REDACT, request_texts(request))

    assert row["error_message"] == REMOVED_ECHO
    assert_clean(stored(row))


def test_request_texts_skip_short_messages() -> None:
    request = ModelRequest(messages=(Message(role="user", content="hi"),), system=SYSTEM)
    assert request_texts(request) == [SYSTEM]


def test_long_text_is_capped_when_stored() -> None:
    attempt = Attempt("ollama", "ProviderBadResponse", "x" * (MAX_TEXT + 100), False)
    reason = "r" * (MAX_TEXT + 100)
    request = make_request()
    row = to_row(request_failed(RouteFailed(reason, [attempt]), request, OPTIONS), REDACT)
    assert len(row["route_reason"]) == MAX_TEXT
    assert len(row["attempts"][0]["message"]) == MAX_TEXT
    row = to_row(provider_unavailable(attempt, request), REDACT)
    assert len(row["error_message"]) == MAX_TEXT


@pytest.mark.parametrize("start", [MAX_TEXT - 10, MAX_TEXT - len(FAKE_SECRET) + 1, MAX_TEXT - 2])
def test_a_secret_across_the_cap_is_redacted_not_cut(start: int) -> None:
    # Redaction runs before the cap (#115): a configured secret that straddles
    # character MAX_TEXT is masked whole, so no prefix of it is stored.
    text = "e" * start + FAKE_SECRET + "e" * 50
    attempt = Attempt("ollama", "ProviderBadResponse", text, False)
    request = make_request()
    rows = [
        to_row(request_failed(RouteFailed(text, [attempt]), request, OPTIONS), REDACT),
        to_row(provider_unavailable(attempt, request), REDACT),
    ]
    texts = [rows[0]["route_reason"], rows[0]["attempts"][0]["message"], rows[1]["error_message"]]
    for t in texts:
        assert len(t) <= MAX_TEXT
        assert FAKE_SECRET[:6] not in t
        # Masked, not cut: the marker (or its start, if the cap falls inside it) is there.
        kept = t[start:].removesuffix("…")[: len(REDACTED)]
        assert kept and REDACTED.startswith(kept)


# --- the model refuses anything that isn't metadata ----------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"prompt": PROMPT},
        {"text": ANSWER},
        {"metadata": {"anything": 1}},
        {"request_id": "has space"},
        {"provider": "Claude!"},
        {"error_type": "Not a type name"},
        {"model": "m" * 201},
        {"input_tokens": -1},
        {"event_type": "chat.transcript"},
        {
            "attempts": tuple(
                {"provider": "x", "error_type": "E", "message": "m", "retryable": True}
                for _ in range(3)
            )
        },
    ],
    ids=lambda b: next(iter(b)),
)
def test_event_rejects_non_metadata(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Event(**{"event_type": "request.failed", "actor": "router", **bad})


def test_table_columns_match_event_fields() -> None:
    assert set(events_table.c.keys()) == set(Event.model_fields) | {"id", "recorded_at"}


# --- migration touches only events objects (offline SQL, no database) -----------------


def offline_sql(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], direction: str
) -> str:
    url = "postgresql+asyncpg://aether:offline-test-pw@127.0.0.1:5432/aether"
    settings = Settings(_env_file=None, database_url=url)  # type: ignore[arg-type]
    monkeypatch.setattr(core.config, "get_settings", lambda: settings)
    config = Config(str(REPO / "alembic.ini"))
    if direction == "upgrade":
        command.upgrade(config, "0001:0002", sql=True)
    else:
        command.downgrade(config, "0002:0001", sql=True)
    return capsys.readouterr().out


def statements(sql: str) -> list[str]:
    # Skip SQL comments and the JSON log lines Alembic writes to the same stdout.
    body = [line for line in sql.splitlines() if line and not line.startswith(("--", "{"))]
    joined = " ".join(" ".join(body).split())
    # Function bodies are $$-quoted; collapse them so ';' inside doesn't split.
    joined = re.sub(r"\$\$.*?\$\$", "$$…$$", joined)
    return [s.strip() for s in joined.split(";") if s.strip()]


EVENTS_OBJECTS = {
    "events",
    "events_occurred_at_idx",
    "events_request_id_idx",
    "events_append_only",
    "events_no_update_delete",
    "events_no_truncate",
}


def test_upgrade_touches_only_events_objects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stmts = statements(offline_sql(monkeypatch, capsys, "upgrade"))
    ddl = [s for s in stmts if not s.startswith(("BEGIN", "COMMIT", "UPDATE alembic_version"))]
    created = set()
    for s in ddl:
        m = re.match(r"CREATE (?:TABLE|INDEX|FUNCTION|TRIGGER) (\w+)", s)
        assert m, f"unexpected statement: {s[:80]}"
        created.add(m.group(1))
        assert re.search(r"\bON events\b", s) or s.startswith(
            ("CREATE TABLE events ", "CREATE FUNCTION")
        ), s[:80]
    assert created == EVENTS_OBJECTS
    assert (
        "UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001'"
        in stmts
    )


def test_downgrade_drops_only_events_objects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stmts = statements(offline_sql(monkeypatch, capsys, "downgrade"))
    ddl = [s for s in stmts if not s.startswith(("BEGIN", "COMMIT", "UPDATE alembic_version"))]
    assert ddl == ["DROP TABLE events", "DROP FUNCTION events_append_only()"]


def test_attempts_none_is_sql_null_not_json_null() -> None:
    # Found against the real database: JSON null breaks the events_attempts CHECK.
    from sqlalchemy.dialects.postgresql import JSONB

    column_type = events_table.c.attempts.type
    assert isinstance(column_type, JSONB)
    assert column_type.none_as_null is True


# --- include_sensitive, the per-request approval (migration 0008, #144) ----------------


@pytest.mark.parametrize("approved", [True, False])
def test_request_events_record_include_sensitive(approved: bool) -> None:
    from core.events import request_crashed
    from router.routing import NoRoute, RouteDecision

    options = RouteOptions(include_sensitive=approved)
    request = make_request()
    decision = RouteDecision("ollama", "local", "local preferred")
    events = [
        request_received(request, options),
        route_selected(decision, request, options),
        request_failed(NoRoute("no route"), request, options),
        request_crashed(RuntimeError("x"), request, options),
    ]
    assert [to_row(e, REDACT)["include_sensitive"] for e in events] == [approved] * 4


def test_a_memory_event_never_carries_include_sensitive() -> None:
    from uuid import uuid4

    with pytest.raises(ValueError, match="include_sensitive"):
        Event(
            event_type="memory.deleted",
            actor="memory",
            memory_record_id=uuid4(),
            memory_category="preference",
            memory_source="owner",
            memory_level="personal",
            include_sensitive=True,
        )


def test_migration_0008_matches_the_memory_event_names() -> None:
    # Its CHECK tells memory events apart by the "memory." prefix.
    from typing import get_args

    from core.events import MEMORY_EVENT_TYPES, EventType

    names: list[str] = []
    for arg in get_args(EventType):
        names.extend(get_args(arg) or [arg])
    flat: list[str] = []
    for name in names:
        flat.extend(get_args(name) or [name])
    assert {n for n in flat if n.startswith("memory.")} == set(MEMORY_EVENT_TYPES)
    source = (REPO / "migrations/versions/0008_event_include_sensitive.py").read_text()
    assert "NOT starts_with(event_type, 'memory.')" in source
    assert "include_sensitive" in events_table.c


def test_events_without_the_approval_leave_the_column_out() -> None:
    # Keeps memory changes and other events working on a database before 0008.
    from core.events import call_completed

    response = ModelResponse(
        text=ANSWER, provider="ollama", model="m", kind="local", latency_ms=1.0
    )
    row = to_row(call_completed(response, make_request()), REDACT)
    assert "include_sensitive" not in row
    assert "include_sensitive" in to_row(request_received(make_request(), OPTIONS), REDACT)


def test_memory_events_are_inserted_without_the_column() -> None:
    import asyncio
    from typing import Any
    from uuid import uuid4

    from core.events import write_memory_event

    seen: list[Any] = []

    class Conn:
        async def execute(self, statement: Any) -> Any:
            seen.append(statement)

            class Result:
                def scalar_one(self) -> int:
                    return 1

            return Result()

    event = Event(
        event_type="memory.deleted",
        actor="memory",
        memory_record_id=uuid4(),
        memory_category="preference",
        memory_source="owner",
        memory_level="personal",
    )
    asyncio.run(write_memory_event(Conn(), event))  # type: ignore[arg-type]
    columns = {
        c.key for c in seen[0].compile().statement.table.c if c.key in seen[0].compile().params
    }
    assert "include_sensitive" not in columns
    assert "memory_level" in columns
