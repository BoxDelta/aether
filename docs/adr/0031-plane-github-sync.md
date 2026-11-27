# ADR-031: Plane as a one-way mirror of GitHub Issues

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
(ADR-030), on the same board, visible there. This ADR
records a preference, not a missing GitHub feature.

The repository is public (ADR-022). Anything synced into a GitHub issue
is public.

## Options

1.  Keep ADR-010 as it is: GitHub only.
2.  Plane as a read-only mirror of GitHub.
3.  One-way mirror built in n8n (chosen).
4.  Plane's built-in two-way GitHub sync. It is a paid Pro feature and
    isn't in the self-hosted Community Edition, so it needs a paid plan.

## Decision

Option 3, with these limits:

-   **GitHub stays the source of truth for agents.** Claude Code, Codex
    and the dispatcher read GitHub only, never Plane's API. ADR-010 still
    applies in every other respect.
-   An n8n workflow ("Aether: GitHub -> Plane mirror") runs every 10
    minutes. It reads open issues and issues closed in the last 3 days
    from `BoxDelta/aether`, skips pull requests, and creates a Plane work
    item in the Aether workspace's Aether project for each new open
    issue. Closed issues move to Done, "not planned" goes to Cancelled,
    and reopened issues go back to Todo. Old closed issues are not
    imported.
-   The GitHub token is fine-grained, read-only (Issues), limited to this
    repository, and expires in 90 days. Nothing is written to GitHub, so
    Plane never creates GitHub issues.
-   **Plane is a view only: edit issues on GitHub.**
-   Plane is reachable on the local machine only.
-   **Privacy:** only the Aether project in the separate aether workspace
    receives mirrored issues. Personal tasks stay in other Plane projects
    and workspaces, which the mirror never touches.
-   Labels `dispatch:auto`, `dispatch:local` and `dispatch:claude` are
    set on GitHub. Their mapping in Plane is display only.
-   Plane's data volume is backed up with the other dispatcher services.

## Consequences

-   \+ One board for everything the owner tracks.
-   \- Plane edits are overwritten on the next GitHub change.
-   \- If n8n is off for more than 3 days, issues closed in that time stay
    open in Plane until edited on GitHub.
-   \- The token expires every 90 days and must be renewed.
-   Everything in the Aether Plane project is a copy of a public GitHub
    issue; nothing typed in Plane is sent to GitHub.

## Revisit when

-   The mirror duplicates or misses issues.
-   Plane's GitHub integration becomes available in the Community Edition.
-   The repository goes private again.
-   Plane stops being used day to day.
