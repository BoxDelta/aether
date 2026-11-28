# ADR-041: Dispatcher identity: a machine user with a classic token

**Status:** Proposed (owner decision 2026-11-27; draft for acceptance)\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

The dispatcher (ADR-030) used a fine-grained personal access token of the
owner's account. Everything it did on GitHub showed as `BoxDelta`, so the
issue history could not tell a label the dispatcher added from one the owner
added. The trust checks (`dispatch:auto`, and `dispatch:local-ok` for the
local path) rest on "the owner added this label", so only the write client's
own limits stopped the dispatcher adding them (#203, from the security review
of step 4b).

## Decision

1.  **A dedicated machine user, `boxdelta-dispatch`**, invited as a
    collaborator (write role) on `BoxDelta/aether` only. Its pull requests
    and labels show as that account. It has its own email address, two-factor
    authentication and stored recovery codes.
2.  **A classic personal access token with only the `public_repo` scope,
    90-day expiry.** Fine-grained tokens can only target repositories owned
    by the token's own account (or an organisation it belongs to), so a
    collaborator can't use one on `BoxDelta/aether`. `public_repo` is the
    smallest classic scope that can push branches, open pull requests and
    add labels on a public repository. It reaches every public repository
    the machine user can write to, which is only this one.
3.  **The approval rule.** `protect-main` requires one approving review, and
    only the owner (admin) is on the bypass list. The machine user can't
    approve its own pull requests, and it isn't on the bypass list, so it
    can't merge, even though `public_repo` with write access could call the
    merge endpoint. The dispatcher's code still has no merge call and a test
    checks it. The owner merges with `safemerge` as before.
4.  **Trust checks read the issue's timeline.** `dispatch:auto` and
    `dispatch:local-ok` count only when the last `labeled` event for that
    label was by the owner's own account: login `DISPATCH_OWNER` and account
    type `User`, never `DISPATCH_BOT_LOGIN` (default `boxdelta-dispatch`).
    A label the machine user adds, or re-adds after the owner, never counts.
    The local path re-checks `dispatch:local-ok` right before each try.
5.  **Token identity check.** Before any local try the run calls `GET /user`.
    The local path is skipped unless the login is the machine user's (never
    the owner's) and the token's scopes are exactly `public_repo`. The run
    prints the token's expiry date.
6.  **Setting the token.** `scripts/dispatch_set_token.py` reads the token
    from standard input only (the owner uses fish's `read -s`, so there is no
    echo and no shell history), checks the login and scope with GitHub,
    replaces `DISPATCH_GITHUB_TOKEN` in the dispatcher's env file (mode 600,
    atomic write) and never prints the token.
7.  **Expiry and rotation.** Tokens expire after 90 days. The owner rotates
    one about a week before expiry: create the new token on the machine
    user, set it with the script, check one run, then delete the old token.
    If a token may be exposed, delete it at once in the machine user's
    settings, then set a new one. GitHub's secret scanning also revokes
    classic tokens it finds in public repositories.

## Alternatives considered

-   **A GitHub App** (the earlier plan in #203). Its actions show as
    `app[bot]`, tokens last an hour and permissions are per repository. But
    it needs a private key on the PC (a long-lived secret, like a token),
    JWT signing (a new dependency or calls to `openssl`), and its own token
    exchange code. Nothing in the dispatcher needs a short-lived token, so
    a machine user is simpler for one owner and one repository.
-   **Keep the owner's token and tag the dispatcher's actions** (for example
    a label body check). Rejected: the history can't be trusted to tell
    them apart.

## Consequences

-   The dispatcher's pull requests and labels show as `boxdelta-dispatch`.
    The owner reviews them like any other pull request.
-   The local path stays off until the owner swaps the token. The Claude
    path only reads, so it keeps working with either token.
-   A stolen token can push branches, open and comment on pull requests and
    issues, and add or remove labels on this repository until it is deleted.
    It can't merge, push to `main` or edit workflows (`protect-main`), and
    its labels never pass the trust checks.
-   One more account to look after: two-factor authentication, recovery
    codes, and the 90-day rotation.

## Limits

-   `public_repo` is wider than the dispatcher needs (it could also close
    issues or edit comments). Only the write client's allowlist limits that
    in practice.
-   Once the owner approves a machine-user pull request, the machine user
    could merge it itself (write access plus an approval). The dispatcher
    has no merge call, so this needs a stolen token; the owner merges
    right after approving, or approves only at merge time.
-   With one approval required, the owner's own pull requests (no second
    reviewer) need the bypass: `safemerge` (`scripts/safemerge.fish`)
    calls `gh pr merge` without `--admin`, so it will need `--admin` or
    the owner merges in the web UI with the bypass. That is an
    owner-approved edit to `scripts/safemerge.fish` (#214).
-   If the repository becomes private, `public_repo` stops working and this
    ADR needs revisiting (the `repo` scope is much wider).
