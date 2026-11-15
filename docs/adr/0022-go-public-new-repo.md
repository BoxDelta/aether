# ADR-022: Go public via a new repo from a clean snapshot

**Status:** Accepted (owner, 2026-11-14)\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Status note

-   The new repo was created on 2026-11-14 as private.
-   The snapshot commit `e48a8c8` was taken from `main` of the private
    repo at `3879f31`.
-   Settings applied while private: read-only workflow permissions,
    Dependabot alerts and security updates. Labels were recreated.
-   Correction: approval for all outside collaborators moved to the
    right-after-the-flip stage, because GitHub does not allow it on
    private repositories.

## Context

AETHER is developed in a private repo. The owner wants to make it public.
Issue #74 holds the pre-flight checklist. The current repo has full
history, issues, PRs and review text written for a private audience, and
docs that name personal and host details. Flipping it to public would
publish all of that.

## Decision

1.  **New repo, clean snapshot.** The public repo is created from a
    snapshot of `main`: one commit, no history, no old issues or PRs. This
    repo is never flipped to public.
2.  **Names and archive.** The new public repo is `BoxDelta/aether`. The
    current repo is first renamed to `aether-private-archive`, then
    archived (not deleted). `scripts/safemerge.fish` hardcodes
    `BoxDelta/aether`, so it keeps working unchanged.
3.  **License.** Apache-2.0, with a `NOTICE` file. The AETHER name is not
    licensed; Apache-2.0 §6 already withholds trademark rights, and the
    NOTICE does not grant any.
4.  **Commit identity.** Commits in the new repo use the owner's GitHub
    noreply address, set as repo-local git config at snapshot time.
5.  **Before going public:**
    -   scrub personal and host details: the OS is written as
        "Arch / GNOME", no CPU or RAM models, generic network and paths;
    -   a security pass, including a Codex review;
    -   then repo settings, in two stages because on the free plan code
        scanning, secret scanning with push protection and enforced
        rulesets are only available on public repos (see step 4 of
        decision 7): read-only workflow permissions and Dependabot while
        private; approval for all outside collaborators, rulesets,
        secret scanning, push protection, code scanning and private
        vulnerability reporting right after the flip (GitHub does not
        allow the fork-PR approval setting on private repositories).
6.  **Agent safety.** Agents act only on issues the owner wrote. Outside
    issues and comments are data, never instructions. The Codex trigger is
    only `@codex review`, never `@codex fix`.
7.  **Order of work.**
    1.  PR 1: this ADR plus `LICENSE` and `NOTICE`.
    2.  PR 2: scrub.
    3.  PR 3: `SECURITY.md`, `CONTRIBUTING.md`, README.
    4.  Create the new repo as private, push the snapshot and re-verify
        Sentinel. While it is private, apply what is available: Actions
        permissions (read-only default) and Dependabot. Flip it public.
        Immediately after the flip, enable approval for all outside
        collaborators, rulesets, secret scanning, push protection, code
        scanning and private vulnerability reporting (`SECURITY.md`
        points reporters to it), and re-check Sentinel. During that
        short window the owner is the only person with write access.

**Ruleset design.** The post-flip rulesets on `main`:

-   require a pull request;
-   require the Sentinel status checks named `checks` and `secrets`;
-   block force pushes and branch deletion;
-   do not require approvals or code-owner review: there is one
    maintainer, agent PRs are authored under the owner's account, and
    GitHub does not let an author approve their own PR;
-   keep the bypass list empty.

When creating the rulesets, set the source of both required checks to
GitHub Actions, so a status with the same name from elsewhere cannot
satisfy them.

Options considered: flipping this repo to public (rejected: exposes
history, issues and PRs); rewriting this repo's history (rejected: needs a
force-push and still carries issues and PRs); MIT (not chosen: Apache-2.0
adds an explicit patent grant and a NOTICE mechanism).

## Consequences

-   Easier: the public surface is small and reviewed; the old repo stays
    as a private record.
-   Issue and PR numbers restart at 1 in the new repo. References like
    `#74` in this repo's docs and commits point to the archive.
-   The project board and labels must be recreated.
-   Dependabot config, Sentinel action pins and the Postgres image digest
    must be re-verified in the new repo.
-   Risks: a missed personal detail or secret in the snapshot is public
    the moment the repo flips, so the scrub and security pass must finish
    first and the repo stays private until then; the rename must happen
    before the new repo is created, or the name is taken. On the free
    plan, rulesets, secret scanning, push protection and code scanning
    can only be enabled once the repo is public, so there is a short
    window after the flip without them; the owner is the only person
    with write access during it, and these settings are the first thing
    done after the flip.
-   Apache-2.0 is hard to undo for copies already distributed.

## Revisit when

The scrub or security pass finds something that cannot be fixed in the
snapshot; the owner wants a different license; or the project takes on
outside contributors who need a CLA or DCO.
