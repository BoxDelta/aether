"""Memory's fixed value sets (ADR-024 §1), in a module that imports nothing from
AETHER, so ``core/events.py`` can type its memory columns with them without an
import cycle (``memory/store.py`` imports ``core/events.py``).

Frozen copies live in migrations 0004 (``memory_records``) and 0005 (``events``);
tests/test_memory_store.py and tests/test_memory_events.py fail if they drift.
"""

from typing import Literal, get_args

Category = Literal["profile", "preference", "project", "decision", "task", "reference"]
Status = Literal["pending", "approved"]
Source = Literal["owner", "model", "tool"]
PrivacySource = Literal["owner", "suggested"]
CATEGORIES: tuple[Category, ...] = get_args(Category)
STATUSES: tuple[Status, ...] = get_args(Status)
SOURCES: tuple[Source, ...] = get_args(Source)
