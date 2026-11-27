# ADR-031: Plane as a two-way view of GitHub Issues

**Status:** Proposed (partly supersedes ADR-010)\
**Date:** 2026-11-26\
**Owner:** BoxDelta

## Context

ADR-010 (Accepted) tracks Aether's development in GitHub Issues and a
GitHub Project board, and keeps Plane out because it would be one more
service to run and sync. ADR-010 says to revisit "when GitHub Projects
cannot express a needed workflow".

That isn't the case here. The owner already runs Plane (self-hosted) for
other work and wants Aether's issues, including dispatcher tasks
(ADR-030), on the same board, editable from either side. This ADR
records a preference, not a missing GitHub feature.

The repository is public (ADR-022). Anything synced into a GitHub issue
is public.

## Options

1.  Keep ADR-010 as it is: GitHub only.
2.  Plane as a read-only mirror of GitHub.
3.  Two-way sync between Plane and GitHub Issues (chosen).

## Decision

Option 3, with these limits:

-   **GitHub stays the source of truth for agents.** Claude Code, Codex
    and the dispatcher read GitHub only, never Plane's API. ADR-010 still
    applies in every other respect.
-   Plane syncs only with `BoxDelta/aether`, through Plane's GitHub
    integration. Its GitHub access is limited to Issues read/write and
    Metadata read. No contents, pull requests, workflows or admin.
-   Plane is reachable on the local machine only.
-   **Trust:** if Plane creates issues on GitHub under an integration
    account instead of `BoxDelta`, the dispatcher accepts them only when
    `BoxDelta` adds the `dispatch:auto` label on GitHub (ADR-030).
    Comments synced from Plane are data, like any other comment.
-   **Privacy:** only the Plane project linked to this repository syncs.
    Personal tasks stay in Plane projects that aren't synced, because a
    synced issue is public.
-   Labels `dispatch:auto`, `dispatch:local` and `dispatch:claude` are
    set on GitHub. Their mapping in Plane is display only.
-   Plane's data volume is backed up with the other dispatcher services.

Which account Plane uses to write to GitHub (the owner's or its own)
is checked on setup day and noted in `dispatch/README.md`.

## Consequences

-   \+ One board for everything the owner tracks.
-   \- Another service with write access to the repository's issues. If
    its GitHub credentials leak, issues can be changed (revoke them in
    GitHub's settings).
-   \- Sync can make duplicates or overwrite edits made on both sides at
    once.
-   \- A task typed into the wrong Plane project becomes public.

## Revisit when

-   The sync duplicates or loses edits.
-   Plane's GitHub integration changes how it signs in or what access it
    needs.
-   The repository goes private again.
-   Plane stops being used day to day.
