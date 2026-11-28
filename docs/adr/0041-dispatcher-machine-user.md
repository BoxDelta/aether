# ADR-041: Dispatcher identity: a machine user with a classic token

**Status:** Proposed (owner decision 2026-11-27; draft for acceptance)\
**Date:** 2026-11-27\
**Owner:** BoxDelta\
**Amends:** ADR-022 (ruleset design) and ADR-018 (bypass list), ADR-030
§Credentials and §Trust

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
    add labels on a public repository. Write access to repositories
    is only this one (the only collaboration). It still lets the account
    open issues, pull requests and comments on any public repository,
    fork, star, and create public repositories of its own. It has no
    `workflow` scope, so it can't push changes to `.github/workflows` on
    any branch.
3.  **The approval rule (#215): two rulesets on `main`.** GitHub counts an
    approval from any account with write access, so a bare "one approval"
    would let a stolen machine-user token approve and merge a pull request,
    or push to a branch after the owner approved. So `main` has two active
    rulesets, split so that the owner's bypass of approvals can never skip
    the checks:
    -   **`checks`**: the Sentinel status checks `checks` and `secrets`
        (source: GitHub Actions), no force pushes, no branch deletion. The
        **bypass list is empty**: nobody skips it, not even the owner.
    -   **`review`**: a pull request is required (nobody pushes to `main`
        directly), one approving review **from a code owner** (`* @BoxDelta`
        is the first rule in `.github/CODEOWNERS`, so only the owner's
        approval counts), stale approvals dismissed when new commits are
        pushed, and the most recent push approved by someone other than its
        pusher. The **only bypass is the repository admin role**, for pull
        requests only, so the owner can merge their own pull requests but
        can't push to `main` directly.

    Then the machine user can't merge, even though `public_repo` with
    write access could call the merge endpoint. The dispatcher's code
    still has no merge call and a test checks it. The owner merges their
    own pull requests with the bypass (`safemerge`, which uses `--admin`
    only for pull requests authored by `BoxDelta`, only with the reviewed
    commit hash, and only after `checks` and `secrets` succeeded; #214),
    and the machine user's after approving them (`safemerge` refuses those
    unless the owner's latest review approves the exact head commit).
    This amends ADR-022 ("Ruleset design": no approvals, no code-owner
    review, empty bypass list) and ADR-018 ("After the public flip":
    CODEOWNERS not enforced) once this ADR is accepted.
4.  **Trust checks read the issue's timeline.** `dispatch:auto` and
    `dispatch:local-ok` count only when the last `labeled` event for that
    label was by the owner's own account: login `DISPATCH_OWNER` and account
    type `User`, never `DISPATCH_BOT_LOGIN` (default `boxdelta-dispatch`).
    A label the machine user adds, or re-adds after the owner, never counts,
    and neither does one an app added for the owner
    (`performed_via_github_app`). A title change by anyone but the owner
    (`renamed` event) fails the trust check.
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
    **At the cut-over**, the owner also deletes their own fine-grained
    dispatcher token right after the script succeeds and one run passes:
    labels added with it would count as the owner's.
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
    issues, edit issue text, and add or remove labels on this repository
    until it is deleted. It can't merge or push to `main` (`protect-main`
    with the rules in decision 3), can't change workflows (no `workflow`
    scope), and its labels never pass the trust checks.
-   It could edit the body of an owner's issue after the owner labelled it,
    so the local model and the brief would read text the owner didn't
    write. Title changes are caught; body edits are not yet (#216).
-   One more account to look after: two-factor authentication, recovery
    codes, and the 90-day rotation.

## Limits

-   `public_repo` is wider than the dispatcher needs (it could also close
    issues or edit comments). Only the write client's allowlist limits that
    in practice.
-   Once the owner approves a machine-user pull request, the machine user
    could merge it itself (write access plus a code-owner approval of the
    latest push). The dispatcher has no merge call, so this needs a stolen
    token; the owner merges right after approving.
-   Any tool holding the owner's own credentials (the gh CLI, Claude Code
    sessions) can still add `dispatch:local-ok` and have it count. That is
    a rule (`CLAUDE.md`), not a permission.
-   Label events carry the label's name, not its id. A label renamed to
    `dispatch:local-ok` after the owner added it under another name is
    assumed to keep the old name in its earlier events (fails closed); not
    yet confirmed on GitHub.
-   Until the token swap (the cut-over), the dispatcher still uses the
    owner's token, so its pull requests show `BoxDelta` as author and
    `safemerge` treats them as the owner's own: the reviewed hash is the
    check. After the swap they show `boxdelta-dispatch`.
-   The owner's own credentials (the gh CLI, Claude Code sessions) are
    admin: they can use the `review` bypass or edit the rulesets. That is
    held by rules (`CLAUDE.md` rule 5, the `gh pr merge` deny rule), not by
    GitHub. Nothing the machine user holds can do it.
-   If the repository becomes private, `public_repo` stops working and this
    ADR needs revisiting (the `repo` scope is much wider).
