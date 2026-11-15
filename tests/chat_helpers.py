"""The ``/chat`` test harness (#37): ``Setup`` builds Core with two fake providers
and an in-memory event store; shared by test_chat, test_auth, test_chat_output_cap
and test_error_bodies.
"""

import io
import json
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from core.app import create_app
from core.config import Settings
from core.events import EventRecorder, EventWriter
from core.identity import load_identity
from core.log import Redactor, secrets_from
from router.contract import FinishReason, ProviderUnavailable, Usage
from router.routing import Router
from tests.fakes import FakeProvider, MemoryWriter, snapshot

FAKE_KEY = "sk-ant-api03-FAKE-chat-test-0123456789"  # gitleaks:allow
PROMPT = "PRIVATE-PROMPT-MARKER what is my bank PIN"
ANSWER = "PRIVATE-ANSWER-MARKER it is 4711"
LOCAL = ("127.0.0.1", 50000)
TOKEN = "fake-api-token-chat-tests-0123456789abcdef"  # noqa: S105  # gitleaks:allow
AUTH = {"authorization": f"Bearer {TOKEN}"}
USAGE = Usage(input_tokens=11, output_tokens=7)


class Setup:
    def __init__(
        self,
        *,
        local_error: Exception | None = None,
        cloud_error: Exception | None = None,
        free_gib: float = 4.0,
        finish_reason: FinishReason = "stop",
        text: str | None = None,
        cloud_cap: str | None = None,
        writer: EventWriter | None = None,
        recorder_timeout_s: float = 3.0,
        client: tuple[str, int] = LOCAL,
    ) -> None:
        self.log = io.StringIO()
        self.local = FakeProvider(
            "ollama",
            "local",
            error=local_error,
            finish_reason=finish_reason,
            text=text,
            answer=ANSWER,
            usage=USAGE,
        )
        self.cloud = FakeProvider(
            "claude",
            "cloud",
            error=cloud_error,
            finish_reason=finish_reason,
            text=text,
            answer=ANSWER,
            usage=USAGE,
        )
        identity = load_identity()
        router = Router([self.local, self.cloud], identity, probe=lambda: snapshot(free_gib))
        settings = Settings(
            _env_file=None,
            anthropic_api_key=SecretStr(FAKE_KEY),
            aether_api_token=SecretStr(TOKEN),
            aether_cloud_max_output_tokens=cloud_cap,
        )
        # The same redactor Core builds, so the token is covered too.
        self.writer = (
            writer if writer is not None else MemoryWriter(Redactor(secrets_from(settings)))
        )
        app = create_app(
            settings,
            identity,
            router=router,
            events=EventRecorder(self.writer, timeout_s=recorder_timeout_s),
        )
        # create_app configured logging to stdout; capture it instead.
        from core.log import configure_logging

        configure_logging(settings, stream=self.log)
        self.client = TestClient(app, client=client, headers=AUTH, raise_server_exceptions=False)

    @property
    def rows(self) -> list[dict[str, Any]]:
        assert isinstance(self.writer, MemoryWriter)
        return self.writer.rows

    def post(self, **body: Any) -> Any:
        payload = {"messages": [{"role": "user", "content": PROMPT}], **body}
        return self.client.post("/chat", json=payload)

    def log_records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.getvalue().splitlines() if line]


def error_with_key_cause(name: str = "ollama") -> ProviderUnavailable:
    """Retryable provider error whose __cause__ carries a key, a traceback and the prompt."""
    try:
        try:
            raise RuntimeError(f"upstream {FAKE_KEY} Traceback (most recent call last) {PROMPT}")
        except RuntimeError as inner:
            raise ProviderUnavailable(name, f"cannot connect to {name}") from inner
    except ProviderUnavailable as exc:
        return exc


def assert_no_content(text: str) -> None:
    for forbidden in (PROMPT, ANSWER, FAKE_KEY, "Traceback", "upstream"):
        assert forbidden not in text, forbidden
