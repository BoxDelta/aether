"""Shared test doubles (#37): one configurable fake provider, the hardware snapshot
helper and an in-memory event writer. Import these instead of writing new copies.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from core.events import Event, EventWriter, forbidden_texts, to_row
from core.log import Redactor, SecretCheck
from router.contract import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    PrivacyLevel,
    Provider,
    ProviderInfo,
    ProviderKind,
    Usage,
)
from router.hardware import UNKNOWN_GPU, GpuState, HardwareSnapshot

GIB = 1024**3
# The tripwire check for test routers: patterns only. Core builds its own with
# SecretCheck.from_settings (tests/test_cloud_tripwire.py checks that).
CHECK = SecretCheck()


def snapshot(free_gib: float | None = 4.0) -> HardwareSnapshot:
    """An 8 GiB GPU with ``free_gib`` free (None: GPU unknown) and 16 GiB of RAM."""
    gpu = UNKNOWN_GPU if free_gib is None else GpuState("nvml", 8 * GIB, int(free_gib * GIB), 10)
    return HardwareSnapshot(gpu=gpu, ram_available_bytes=16 * GIB)


class FakeProvider(Provider):
    """A provider that answers, refuses or raises, and counts what it was asked.

    - ``error``: raised by every call (after ``delay_s``).
    - ``text``: the exact answer text. If not set, the answer is ``answer`` (default
      ``"answer from <name>"``), or ``""`` when ``finish_reason="refusal"``.
    - ``usage``: token counts on the response (default none).
    - ``available``: what ``check_available()`` says.
    - ``memory_limit``: ``ProviderInfo.memory_limit`` (default none: no memory).

    ``calls`` counts calls, ``requests`` keeps each request, ``checks`` counts
    availability checks.
    """

    def __init__(
        self,
        name: str,
        kind: ProviderKind,
        *,
        error: Exception | None = None,
        finish_reason: FinishReason = "stop",
        text: str | None = None,
        answer: str | None = None,
        usage: Usage | None = None,
        available: bool = True,
        delay_s: float = 0.0,
        memory_limit: PrivacyLevel | None = None,
    ) -> None:
        super().__init__(
            ProviderInfo(
                name=name,
                kind=kind,
                default_model=f"{name}-model",
                max_privacy="sensitive" if kind == "local" else "personal",
                memory_limit=memory_limit,
            )
        )
        self.error = error
        self.finish_reason = finish_reason
        self.text = text
        self.answer = answer if answer is not None else f"answer from {name}"
        self.usage = usage
        self.available = available
        self.delay_s = delay_s
        self.calls = 0
        self.checks = 0
        self.requests: list[ModelRequest] = []

    async def _generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        if self.text is not None:
            text = self.text
        else:
            text = "" if self.finish_reason == "refusal" else self.answer
        return ModelResponse(
            text=text,
            provider=self.name,
            model=self.info.default_model,
            kind=self.info.kind,
            finish_reason=self.finish_reason,
            usage=self.usage,
        )

    async def check_available(self) -> bool:
        self.checks += 1
        return self.available


class MemoryWriter(EventWriter):
    """Stores exactly the row values the database writer would insert."""

    def __init__(self, redactor: Redactor) -> None:  # no engine: never touches a DB
        self._redact = redactor
        self.rows: list[dict[str, Any]] = []

    async def write_all(
        self,
        events: Sequence[Event],
        *,
        request: ModelRequest | None = None,
        also_forbidden: Sequence[str] = (),
    ) -> list[int]:
        forbidden = forbidden_texts(request, also_forbidden)
        self.rows.extend(to_row(e, self._redact, forbidden) for e in events)
        return list(range(len(self.rows)))
