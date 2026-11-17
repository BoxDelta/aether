"""Memory (ADR-024): the store for ``memory_records`` (``memory.store``) and its
fixed value sets (``memory.types``). No API or retrieval yet.

Imports nothing on purpose: ``core/events.py`` imports ``memory.types``, and
``memory.store`` imports ``core/events.py``, so an eager import here would be a cycle.
"""
