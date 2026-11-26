import asyncio
import io
import json
import logging
from typing import Any

import httpx
import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient
from pydantic import SecretStr

from core.config import Settings
from core.identity import Identity, load_identity, render_system_prompt
from core.log import SecretCheck, configure_logging
from router.claude import ClaudeProvider
from router.contract import (
    Message,
    ModelRequest,
    ModelResponse,
    PrivacyNotAllowed,
    Provider,
    ProviderUnavailable,
)
from router.identity_call import apply_identity, call_with_identity
from router.ollama import OllamaProvider

FAKE_KEY = "sk-ant-api03-FAKE-test-key-0123456789abcdef"  # gitleaks:allow
USER_TEXT = "What is the capital of France? PRIVATE-PROMPT-MARKER"
ANSWER_TEXT = "Paris. PRIVATE-ANSWER-MARKER"


def make_identity(**overrides: Any) -> Identity:
    data: dict[str, Any] = {
        "name": "Aether",
        "version": 7,
        "role": "A personal assistant.",
        "tone": "Direct and concise.",
        "principles": ["Report what actually happened.", "Say when you do not know."],
        "user_context_policy": {"include_by_default": ["public", "personal"]},
        "model_notes": {},
    }
    data.update(overrides)
    return Identity.model_validate(data)


def request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(messages=(Message(role="user", content=USER_TEXT),), **kwargs)


# --- Prompt building -------------------------------------------------------


def test_prompt_contains_identity_parts_in_order() -> None:
    prompt = render_system_prompt(make_identity())
    assert prompt.startswith("You are Aether. A personal assistant.")
    assert "Tone: Direct and concise." in prompt
    assert "1. Report what actually happened.\n2. Say when you do not know." in prompt
    assert "Task instructions" not in prompt
    assert "Relevant memory" not in prompt


def test_sections_follow_identity_then_memory_then_task() -> None:
    prompt = render_system_prompt(
        make_identity(), memory_context="Owner likes fish shell.", task="Answer in French."
    )
    identity_at = prompt.index("You are Aether")
    memory_at = prompt.index("Relevant memory:\nOwner likes fish shell.")
    task_at = prompt.index("Task instructions:\nAnswer in French.")
    assert identity_at < memory_at < task_at


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_task_and_memory_are_left_out(blank: str | None) -> None:
    prompt = render_system_prompt(make_identity(), memory_context=blank, task=blank)
    assert prompt == render_system_prompt(make_identity())


def test_model_note_applies_only_to_its_provider() -> None:
    identity = make_identity(model_notes={"ollama": "Keep answers short."})
    assert "Notes for this model: Keep answers short." in render_system_prompt(
        identity, provider="ollama"
    )
    assert "Keep answers short." not in render_system_prompt(identity, provider="claude")
    assert "Keep answers short." not in render_system_prompt(identity)


def test_real_identity_file_renders() -> None:
    identity = load_identity()
    prompt = render_system_prompt(identity, provider="claude")
    assert prompt.startswith(f"You are {identity.name}.")
    for principle in identity.principles:
        assert principle in prompt


def test_apply_identity_keeps_caller_system_as_task_section() -> None:
    original = request(system="Answer in French.")
    prepared = apply_identity(original, make_identity(), "ollama")
    assert prepared.system is not None
    assert prepared.system.startswith("You are Aether.")
    assert prepared.system.endswith("Task instructions:\nAnswer in French.")
    assert prepared.messages == original.messages
    assert original.system == "Answer in French."  # the caller's request is not changed


# --- Both adapters receive the identity prompt ------------------------------


class OllamaRecorder:
    def __init__(self, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(req.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b",
                "created_at": "2026-11-14T00:00:00Z",
                "message": {"role": "assistant", "content": ANSWER_TEXT},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 30,
                "eval_count": 5,
            },
        )


class ClaudeRecorder:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(req.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": ANSWER_TEXT}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "stop_details": None,
                "usage": {"input_tokens": 30, "output_tokens": 5},
            },
        )


def ollama(recorder: OllamaRecorder) -> OllamaProvider:
    return OllamaProvider(
        "http://localhost:11434", "llama3.1:8b", transport=httpx.MockTransport(recorder)
    )


def claude(recorder: ClaudeRecorder) -> ClaudeProvider:
    client = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder))
    return ClaudeProvider(SecretStr(FAKE_KEY), "claude-opus-5-5", http_client=client, max_retries=0)


def call(provider: Provider, req: ModelRequest, identity: Identity) -> ModelResponse:
    return asyncio.run(call_with_identity(provider, req, identity, secret_check=SecretCheck()))


def test_ollama_receives_identity_prompt_as_system_message() -> None:
    rec = OllamaRecorder()
    identity = make_identity()
    response = call(ollama(rec), request(system="Answer in French."), identity)

    assert response.text == ANSWER_TEXT
    messages = rec.bodies[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == render_system_prompt(
        identity, provider="ollama", task="Answer in French."
    )
    assert messages[1] == {"role": "user", "content": USER_TEXT}


def test_claude_receives_identity_prompt_as_system() -> None:
    rec = ClaudeRecorder()
    identity = make_identity()
    call(claude(rec), request(system="Answer in French."), identity)

    assert rec.bodies[0]["system"] == render_system_prompt(
        identity, provider="claude", task="Answer in French."
    )
    assert rec.bodies[0]["messages"] == [{"role": "user", "content": USER_TEXT}]


def test_identity_is_applied_even_without_caller_system() -> None:
    o_rec, c_rec = OllamaRecorder(), ClaudeRecorder()
    identity = make_identity()
    call(ollama(o_rec), request(), identity)
    call(claude(c_rec), request(), identity)

    # Same identity on both providers (no model notes configured).
    expected = render_system_prompt(identity)
    assert o_rec.bodies[0]["messages"][0] == {"role": "system", "content": expected}
    assert c_rec.bodies[0]["system"] == expected


# --- Logging ----------------------------------------------------------------


def call_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "aether.router.call"]


def all_text(record: logging.LogRecord) -> str:
    return record.getMessage() + " " + " ".join(repr(v) for v in vars(record).values())


@pytest.mark.parametrize("which", ["ollama", "claude"])
def test_successful_call_logs_identity_version_and_metadata(
    which: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aether.router.call")
    provider: Provider = ollama(OllamaRecorder()) if which == "ollama" else claude(ClaudeRecorder())
    call(provider, request(), make_identity())

    [record] = call_records(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == "Model call"
    fields = vars(record)
    assert fields["identity_name"] == "Aether"
    assert fields["identity_version"] == 7
    assert fields["provider"] == which
    assert fields["provider_kind"] == ("local" if which == "ollama" else "cloud")
    assert fields["outcome"] == "ok"
    assert fields["privacy"] == "personal"
    assert fields["finish_reason"] == "stop"
    assert (fields["input_tokens"], fields["output_tokens"]) == (30, 5)
    assert fields["latency_ms"] >= 0


@pytest.mark.parametrize("which", ["ollama", "claude"])
def test_debug_logs_never_contain_prompt_answer_or_identity_text(which: str) -> None:
    # Configured the way Core runs it, at the most verbose level. The anthropic SDK
    # logs full request bodies at DEBUG; configure_logging must keep that out.
    stream = io.StringIO()
    configure_logging(Settings(_env_file=None, log_level="DEBUG"), stream=stream)
    provider: Provider = ollama(OllamaRecorder()) if which == "ollama" else claude(ClaudeRecorder())
    call(provider, request(system="SECRET-TASK-MARKER"), make_identity())

    output = stream.getvalue()
    assert '"message": "Model call"' in output  # logging works at all
    for marker in ("PRIVATE-PROMPT-MARKER", "PRIVATE-ANSWER-MARKER", "SECRET-TASK-MARKER"):
        assert marker not in output
    assert "Report what actually happened" not in output
    assert FAKE_KEY not in output


def test_failed_call_is_logged_with_safe_error_and_reraised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="aether.router.call")
    with pytest.raises(ProviderUnavailable):
        call(ollama(OllamaRecorder(status=500)), request(), make_identity())

    [record] = call_records(caplog)
    fields = vars(record)
    assert record.levelno == logging.WARNING
    assert fields["outcome"] == "error"
    assert fields["identity_version"] == 7
    assert fields["error_type"] == "ProviderUnavailable"
    assert fields["error"] == "Ollama server error (HTTP 500)"
    assert fields["retryable"] is True
    assert "boom" not in all_text(record)  # Ollama's own error text is never copied


def test_privacy_refusal_is_logged_and_nothing_is_sent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="aether.router.call")
    rec = ClaudeRecorder()
    with pytest.raises(PrivacyNotAllowed):
        call(claude(rec), request(privacy="sensitive"), make_identity())

    assert rec.bodies == []
    [record] = call_records(caplog)
    assert vars(record)["error_type"] == "PrivacyNotAllowed"
    assert vars(record)["privacy"] == "sensitive"
