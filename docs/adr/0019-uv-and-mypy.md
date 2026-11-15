# ADR-019: uv for Python tooling, mypy for type checks

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

ADR-017 chose Python + FastAPI and left the package manager open and
the type checker as "mypy or pyright". Sentinel needs one reproducible
setup for local runs and CI.

## Decision

- **uv** manages the virtual environment, dependencies and the lockfile
  (`uv.lock`, committed). Commands run as `uv run <tool>`.
- **mypy** with the pydantic plugin is the type checker.

Options considered: pip + venv (no lockfile, slower), Poetry (slower,
heavier); pyright (fast, but pydantic support relies on dataclass
transforms rather than a dedicated plugin).

## Consequences

One command set works the same locally (fish) and in GitHub Actions.
Contributors need uv installed. mypy is slower than pyright on large
codebases, which does not matter at this size.

## Revisit when

uv or mypy stops being maintained, or type-check time becomes a
problem in Sentinel.
