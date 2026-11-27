"""Memory for one model call, as the router sees it (ADR-024 §3, step 12 of #58, #2).

The router never reads memory itself: ``memory/`` depends on ``core/events.py``,
which depends on the router. It gets a ``MemorySource`` (``memory.retrieval.
MemoryRetriever`` in Core) and asks it, per call and after the route is chosen,
for the memory that call may get. What comes back is a ``MemoryContext``: the
record IDs and levels, the rendered ``memory_context`` and the texts for the echo
backstop. Never logged; the repr shows counts and IDs only.

``check_memory`` is the backstop in ``call_with_identity``: it checks every level
in a ``MemoryContext`` against the provider's own memory limit, ``allow_cloud`` and
``include_sensitive``, independently of the selection, and refuses the call with
``MemoryNotAllowed`` before anything is sent if one doesn't fit.
"""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from router.contract import (
    NON_SECRET_LEVELS,
    SECRET,
    MemoryNotAllowed,
    PrivacyLevel,
    ProviderInfo,
    privacy_rank,
)

_SENSITIVE: PrivacyLevel = NON_SECRET_LEVELS[-1]


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """The memory selected for one call, best first."""

    record_ids: tuple[UUID, ...]
    levels: tuple[PrivacyLevel, ...]  # one per record, same order
    context: str | None = field(repr=False)  # for render_system_prompt; None if empty
    texts: tuple[str, ...] = field(repr=False)  # content and rendered lines (echo check)
    chars: int = 0
    left_out: int = 0  # by the privacy rules or the size limits

    def __post_init__(self) -> None:
        if len(self.record_ids) != len(self.levels):
            raise ValueError("one level per record")
        if (self.context is None) != (not self.record_ids):
            raise ValueError("a context exactly when there are records")

    @property
    def highest_level(self) -> PrivacyLevel | None:
        if not self.levels:
            return None
        return max(self.levels, key=privacy_rank)

    def count_by_level(self) -> dict[PrivacyLevel, int]:
        counts = dict.fromkeys(NON_SECRET_LEVELS, 0)
        for level in self.levels:
            counts[level] += 1
        return counts


NO_MEMORY = MemoryContext(record_ids=(), levels=(), context=None, texts=())


def effective_privacy(request_level: PrivacyLevel, memory: MemoryContext | None) -> PrivacyLevel:
    """The request's level raised to the highest level of its memory (ADR-024 §3)."""
    highest = memory.highest_level if memory is not None else None
    if highest is None or privacy_rank(request_level) >= privacy_rank(highest):
        return request_level
    return highest


def fetch_level(limit: PrivacyLevel, *, include_sensitive: bool) -> PrivacyLevel:
    """The highest level worth reading for a call: ``limit``, but below Sensitive
    without ``include_sensitive``, so Sensitive rows can't fill the candidate slots
    and crowd out records the call may get (#140)."""
    if not include_sensitive and privacy_rank(limit) >= privacy_rank(_SENSITIVE):
        return NON_SECRET_LEVELS[NON_SECRET_LEVELS.index(_SENSITIVE) - 1]
    return limit


class MemorySource(Protocol):
    """Where the router gets memory (``memory.retrieval.MemoryRetriever``)."""

    async def context_for(
        self,
        request_text: str | None,
        *,
        limit: PrivacyLevel,
        include_sensitive: bool,
        max_records: int,
        max_chars: int,
    ) -> MemoryContext: ...


def check_memory(
    info: ProviderInfo,
    memory: MemoryContext | None,
    *,
    allow_cloud: bool,
    include_sensitive: bool,
) -> None:
    """Raise ``MemoryNotAllowed`` if any record in ``memory`` may not go to this
    provider in this call. The limit comes from ``info``, not from the caller."""
    if memory is None or not memory.levels:
        return
    if not isinstance(allow_cloud, bool) or not isinstance(include_sensitive, bool):
        raise TypeError("allow_cloud and include_sensitive must be true or false")
    limit = info.memory_limit_for(allow_cloud=allow_cloud)
    for level in memory.levels:
        if (
            limit is None
            or level == SECRET
            or level not in NON_SECRET_LEVELS
            or privacy_rank(level) > privacy_rank(limit)
            or (level == _SENSITIVE and not include_sensitive)
        ):
            raise MemoryNotAllowed(info.name)
