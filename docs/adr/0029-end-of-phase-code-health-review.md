# ADR-029: End-of-phase code-health review

**Status:** Proposed\
**Date:** 2026-11-26\
**Owner:** BoxDelta

## Context

One person builds AETHER with AI coding assistants, so code can grow
faster than anyone reads it. Ruff already enforces structure limits
(#26): mccabe complexity at most 10, and the `PLR0911`, `PLR0912`,
`PLR0913` and `PLR0915` limits on returns, branches, arguments and
statements, plus dead-code rules (`ERA`, `ARG`). Those catch size, not
design. At the end of Phase 1 a read-only code-health review found
smaller problems that were filed as issues instead of fixed on the spot:
#36 (`create_app()` reconfigures global logging), #39 (log field names,
one record per provider failure), #40 (smaller findings backlog) and #52
(a duplicate `FakeProvider` in tests). They are still open while Phase 2
is under way.

## Options

1.  No scheduled review; rely on ruff and per-PR review.
2.  A report-only code-health review at the end of each phase, with its
    findings filed as issues and the important ones fixed before the
    next phase starts.
3.  Block merges on an AI code-health review in CI.

## Decision

Option 2.

-   **Ruff limits stay the automatic floor.** The limits in
    `pyproject.toml` stay as they are and fail Sentinel. A `noqa` for a
    limit carries a reason in the comment, as `core/backup.py` does.
-   **Review at each phase exit.** Before a phase is marked done
    (docs/04, "Phase exit steps"), run one read-only review of the
    phase's code: structure, duplication, misleading names or comments,
    dead code, and module boundaries. It reports only and changes no
    code. Findings become issues labelled with the phase (`bug` or
    `enhancement`); small ones may share one backlog issue.
-   **Tidy before the next phase builds on it.** Findings that the next
    phase's code would build on are fixed first. Phase 1's open findings
    (#36, #39, #40, #52) are handled before Phase 2 exits. Fixes are
    small, behaviour-preserving PRs with the existing tests unchanged.
-   **Advisory, not a gate.** The review is not part of Sentinel and
    blocks nothing; an automated AI reviewer in CI stays Future
    (docs/04).

## Consequences

-   Easier: problems are found while the phase is fresh, and the next
    phase starts from tidy code.
-   Harder: each phase exit takes longer, and some findings will be
    judgement calls.
-   Risks: a report-only review can be ignored. The phase-exit step and
    the open issues make that visible rather than preventing it.

## Revisit when

Ruff's limits produce many `noqa` exceptions; the review finds little
twice in a row; or an automated AI reviewer is added to Sentinel.
