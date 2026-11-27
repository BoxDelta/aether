"""Exit 1 of Phase 2 with the shared fakes (ADR-024 §8, step 14 of #58): a fact
saved while one provider handled a request is recalled when the other provider
handles a later one. The fakes take their memory limits from the real adapters,
so these tests follow the real configuration. All content is invented canaries.

The live version is tests/test_memory_recall_live.py.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from memory.store import MemoryRecord
from router.claude import ClaudeProvider
from router.contract import PrivacyLevel, ProviderUnavailable
from router.ollama import OllamaProvider
from tests.chat_helpers import Setup
from tests.test_memory_chat import FakeSource

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
CANARY = "canary-recall-41d7"
CLOUD_LIMIT = ClaudeProvider(None, "m").info.memory_limit
TRUSTED_LIMIT = OllamaProvider("http://localhost:11434", "m").info.memory_limit


def proposed_from(provider: str, request_id: str, level: PrivacyLevel, n: int = 1) -> MemoryRecord:
    """A fact a model proposed during ``request_id`` and the owner approved."""
    values: dict[str, Any] = {
        "id": uuid4(),
        "content": f"{CANARY} {level} fact {n} from {provider}",
        "key": None,
        "category": "project",
        "privacy": level,
        "status": "approved",
        "source": "model",
        "source_provider": provider,
        "source_request_id": request_id,
        "privacy_source": "owner",
        "created_at": NOW - timedelta(minutes=5),
        "updated_at": NOW - timedelta(minutes=5),
        "approved_at": NOW - timedelta(minutes=4),
        "expires_at": None,
    }
    return MemoryRecord(**values)


def setup(source: FakeSource, **kw: Any) -> Setup:
    return Setup(
        memory=source,
        local_memory_limit=TRUSTED_LIMIT,
        cloud_memory_limit=CLOUD_LIMIT,
        **kw,
    )


def retrieved(s: Setup) -> list[dict[str, Any]]:
    return [r for r in s.rows if r["event_type"] == "memory.context.retrieved"]


def test_the_real_limits_are_the_ones_under_test() -> None:
    assert (CLOUD_LIMIT, TRUSTED_LIMIT) == ("personal", "sensitive")


def test_saved_via_local_recalled_via_cloud() -> None:
    source = FakeSource([])
    first = setup(source)
    a = first.post()
    assert a.status_code == 200 and a.json()["provider"] == "ollama"
    # The owner approves facts proposed from request A (public and personal).
    facts = [
        proposed_from("ollama", a.json()["request_id"], "public"),
        proposed_from("ollama", a.json()["request_id"], "personal"),
    ]
    source.records.extend(facts)

    # Request B: local unavailable, cloud allowed.
    second = setup(source, local_error=ProviderUnavailable("ollama", "down"))
    b = second.post(allow_cloud=True)

    assert b.status_code == 200 and b.json()["provider"] == "claude"
    system = second.cloud.requests[0].system or ""
    memory_section = system.split("Relevant memory:", 1)[1]
    assert all(f.content in memory_section for f in facts)
    cloud_rows = [r for r in retrieved(second) if r["provider"] == "claude"]
    assert len(cloud_rows) == 1
    assert set(cloud_rows[0]["memory_record_ids"]) == {f.id for f in facts}


def test_saved_via_cloud_recalled_locally() -> None:
    source = FakeSource([])
    first = setup(source, free_gib=0.1)  # no local headroom: the cloud answers
    a = first.post(allow_cloud=True)
    assert a.status_code == 200 and a.json()["provider"] == "claude"
    fact = proposed_from("claude", a.json()["request_id"], "personal")
    source.records.append(fact)

    second = setup(source)
    b = second.post()

    assert b.status_code == 200 and b.json()["provider"] == "ollama"
    system = second.local.requests[0].system or ""
    assert fact.content in system.split("Relevant memory:", 1)[1]
    rows = retrieved(second)
    assert [(r["provider"], list(r["memory_record_ids"])) for r in rows] == [("ollama", [fact.id])]


def test_sensitive_facts_stay_local_after_failover() -> None:
    source = FakeSource(
        [proposed_from("ollama", "req-a", lvl) for lvl in ("public", "personal", "sensitive")]
    )
    s = setup(source, local_error=ProviderUnavailable("ollama", "down"))

    resp = s.post(allow_cloud=True, include_sensitive=True)

    assert resp.status_code == 200 and resp.json()["provider"] == "claude"
    local_system = s.local.requests[0].system or ""
    cloud_system = s.cloud.requests[0].system or ""
    assert f"{CANARY} sensitive" in local_system
    assert f"{CANARY} sensitive" not in cloud_system
    assert f"{CANARY} personal" in cloud_system and f"{CANARY} public" in cloud_system
    cloud_row = next(r for r in retrieved(s) if r["provider"] == "claude")
    assert cloud_row["memory_count_sensitive"] == 0
    assert cloud_row["memory_effective_level"] == "personal"


def test_cloud_without_allow_cloud_never_gets_memory() -> None:
    source = FakeSource([proposed_from("ollama", "req-a", "public")])
    s = setup(source, local_error=ProviderUnavailable("ollama", "down"))

    resp = s.post()  # no allow_cloud: no failover, and the cloud is never asked

    assert resp.status_code == 502
    assert s.cloud.calls == 0
    assert [r["provider"] for r in retrieved(s)] == ["ollama"]


def test_failover_to_cloud_with_a_secret_shaped_memory_is_refused() -> None:
    key = "sk-ant-api03-FAKE-recall-tripwire-0123456789"  # gitleaks:allow
    rec = proposed_from("ollama", "req-a", "personal")
    rec = rec.model_copy(update={"content": f"{CANARY} note {key}"})
    s = setup(FakeSource([rec]), local_error=ProviderUnavailable("ollama", "down"))

    resp = s.post(allow_cloud=True)

    assert resp.status_code == 422
    assert resp.json()["error"]["type"] == "SecretContentRefused"
    assert s.cloud.calls == 0
    assert key not in resp.text and CANARY not in resp.text
