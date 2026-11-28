# ADR-042: Discord bot (step 4d) and the owner proof

**Status:** Proposed (owner decisions 2026-11-28 on #228; draft for acceptance)\
**Date:** 2026-11-28\
**Owner:** BoxDelta\
**Amends (once accepted):** ADR-030 §Trust (a second way for an issue to
count as the owner's), §Credentials (a separate bot env file; the Discord
token is not in `dispatch.env`) and §Local path (the dispatcher never adds
`dispatch:auto`; the bot, a separate program, does); ADR-041 decision 4;
ADR-022 decision 6 and `CLAUDE.md` rule 12 (agents act only on issues the
owner wrote), which `/work-on` will read as "written by the owner, or opened
by the bot with a valid proof". `CLAUDE.md` is edited by the owner before the
`/work-on` PR.

## Context

Step 4d of the dispatcher (ADR-030) is a Discord bot: the owner types a task
in `#aether-dispatch`, the bot creates a labelled issue, and the dispatcher
picks it up. The bot posts as the machine user `boxdelta-dispatch`
(ADR-041), and under ADR-041 nothing that account does counts as the
owner's: the issue must be opened by `BoxDelta`, and the trust labels must
be added by `BoxDelta`. So an issue made by the bot would never be picked up,
and if it were, a stolen machine-user token could create trusted issues. The
bot needs a way to show that a request came from the owner that a stolen
GitHub token cannot copy.

## Decision

1.  **A separate process, outbound only.** The bot is `python -m dispatch bot`,
    run by its own systemd user unit. It uses `discord.py` over Discord's
    gateway (one outbound websocket) and HTTPS calls to `discord.com` and
    `api.github.com`. **It opens no listening port**: no HTTP interactions
    endpoint, no webhook receiver, no local API. The runbook checks this
    with `ss -ltnp`. Core never imports it.
2.  **Input: one slash command, `/task`.** Options: `title`, `details`,
    `files` (optional, up to 5). Slash commands only, so the bot needs no
    Message Content intent and parses no free text. Discord delivers the
    command over the bot's authenticated gateway connection with the
    user ID, channel and server set by Discord, not by the message text.
3.  **Who is accepted.** The bot acts only if all of these hold, and
    otherwise ignores the request without replying and without logging its
    text: the numeric `guild_id` and `channel_id` equal the values in the
    bot's env file (names are never compared); `interaction.user.id` equals
    the owner's numeric Discord ID in that file; the user is not a bot or
    webhook. DMs and any interaction without a server are refused. The
    command is registered for server context only (no user-install or DM
    contexts) and restricted in Discord's own permissions. "Public Bot" is
    off, so nobody else can invite it, and the gateway intents are
    `guilds` only (no members, presence or message content).
4.  **Route: `claude` only at first.** Every bot issue is a Claude-path
    issue (`dispatch:claude-only`). A local route, and dropping the
    `dispatch:local-ok` requirement, come later, after about ten clean
    local pull requests, in a new decision. Until then a valid proof can
    never stand in for `dispatch:local-ok`.
5.  **The owner proof, bound to the issue.** The bot creates the issue,
    then edits its body once to end with one line:
    `<!-- aether-proof v1 ts nonce mac -->`. `mac` is HMAC-SHA256 with the
    key `PROOF_KEY` (32 random bytes). The MAC input is a fixed domain
    string, then these fields in order, each as UTF-8 bytes with a
    fixed-width 8-byte big-endian length in front: repository
    (`owner/name`), issue number, owner's Discord ID, route (`claude`),
    `ts`, `nonce`, the title, and the exact body text before the proof line,
    taken as one field and never re-parsed. The owner's ID, the repository
    and the issue number are not in the visible line (the verifier supplies
    them from its configuration and the issue itself), so the public issue
    does not show the owner's Discord ID. The bot writes LF line endings,
    and refuses a title, details or files containing `<!--`, `-->`,
    `aether-proof` or a carriage return. Test vectors are in the tests for
    `dispatch/proof.py`; any mismatch, including from GitHub changing line
    endings, fails closed.
6.  **The dispatcher accepts a bot-made issue as the owner's only if**
    all of these hold, each checked again before every step, and the brief
    is built from the same fetch that was verified:
    -   the author is exactly `boxdelta-dispatch`;
    -   the proof line is the last line and the `mac` verifies, with a
        constant-time compare, for this repository and this issue number
        and the title and body as they are now. An edit to the title or
        body by anyone fails, and so does copying the title and body onto
        another issue, because the issue number differs; no used-ID state
        is needed;
    -   the route is `claude`, and the timestamp is not in the future and
        not older than 7 days. After that the owner opens a new issue: a
        bot-opened issue never passes the label path, so adding labels to
        it by hand does nothing.

    For such an issue the proof stands in for `dispatch:auto` and
    `dispatch:claude-only`. The labels the bot also adds are only there so
    the dispatcher's label query finds the issue; their actor is ignored.
    The existing rules stay: a `dispatch:local-ok` the bot adds never
    counts, and a bot-opened issue without a valid proof is skipped. The
    bot also keeps the Discord interaction IDs it has served, and creates
    one issue per interaction.
7.  **`/work-on` uses the same check** (a follow-up PR; the command is on
    the ask list). It accepts an issue opened by `boxdelta-dispatch` only
    when `python -m dispatch verify <n>` succeeds, and keeps refusing every
    other author. `verify` runs only from the main-only clone
    (`~/Projects/aether-dispatch`) with the fixed env file path, never from
    a working branch, and **prints the verified title and body**; `/work-on`
    uses only that text, so an edit between the check and the read changes
    nothing. The session is not given the key: a deny rule for
    `~/.config/aether-dispatch/**` (an owner-approved `.claude/settings.json`
    edit) goes in the same PR. HMAC is symmetric, so anything that can verify
    can also sign; that is why the key sits only in the two env files and
    `verify` only runs from reviewed, merged code.
8.  **Credentials**, all outside the repository, each revocable on its own:
    -   `DISCORD_BOT_TOKEN`, a second classic `public_repo` token of
        `boxdelta-dispatch` (used only by the bot) and the owner, server and
        channel IDs, in `~/.config/aether-dispatch/bot.env` only;
    -   `PROOF_KEY` in `bot.env` (to sign) and `dispatch.env` (to verify).
        The dispatcher holds neither the Discord token nor the bot's GitHub
        token.
    -   Both files must be mode 600 and not symlinks, or the program refuses
        to start (as `dispatch/config.py` does). A setter script like
        `scripts/dispatch_set_token.py` (stdin only, never printed) sets the
        secrets. To rotate `PROOF_KEY`, change both files together; proofs in
        flight fail, and the owner re-sends those tasks.
    -   The bot's GitHub client has an allowlist: create an issue, edit that
        issue's body once, add the two labels. Anything else (comments,
        other labels, other issues, merges) is refused in code and a test
        checks it.
    -   The variable names go into `dispatch/README.md` when the bot is
        built.
9.  **Privacy and logs.** Replies are visible only to the owner and carry
    the issue number only (`#<n> created`). **The task text becomes a public
    GitHub issue** (ADR-022), readable and kept in history by anyone, so the
    docs/05 §10.4 rules apply: no personal details, no secrets, no memory
    data. The command's help text says so. Task text also passes through
    Discord, which is not end-to-end encrypted (the Telegram rules of
    ADR-027 apply). Logs hold IDs and counts, never task text, and never the
    `mac` or the proof line; the `discord.gateway` and `discord.http`
    loggers are held at INFO or above, and the Discord token, the bot's
    GitHub token and `PROOF_KEY` are registered with the log redactor.
    Limits: 5 tasks per hour, title up to 120 characters, details up to
    2000.
10. **Dependency.** `discord.py`, in a `dispatch` dependency group only,
    pinned to an exact version, covered by Dependabot (the existing `uv`
    entry reads `pyproject.toml`; the one-week cooldown applies) and by
    `pip-audit --all-groups` in Sentinel. The docs/03 §8 checklist is
    answered below.

### docs/03 §8 checklist for `discord.py`

-   Actively maintained: yes, version 2.x with regular releases; the
    release date is checked when the version is pinned.
-   License: MIT.
-   API fit: Discord gateway and application commands (slash commands).
-   Data leaving the machine: the owner's `/task` text and the bot's
    replies go through Discord; the bot token authenticates the bot. No
    memory data, secrets or personal records (decision 9).
-   Self-hosted: Discord is a hosted service; the bot runs on this PC.
-   Costs and limits: free. Discord's rate limits are far above 5 tasks
    an hour.
-   Replaceable: yes. The proof and the issue creation do not depend on
    the library; another client could call them.
-   Runs on this PC: `discord.py` is pure Python but brings `aiohttp` and
    its transitive packages (`multidict`, `yarl`, `frozenlist`,
    `propcache`), some with compiled parts, none in `uv.lock` yet. When the
    version is pinned, list them with their licences, and check the lot
    against the project's Python (`.python-version`, 3.14) including any
    backport package it asks for.
-   New privileged execution path: **yes**. The bot can create issues the
    dispatcher trusts. That is what decisions 3 to 6 limit. It has no merge
    call, no Core access and no memory access.
-   Failure isolation: its own process and unit, in a separate dependency
    group; Core and the dispatcher's timer do not import it.

## Alternatives considered

-   **The bot makes an unlabelled issue and the owner adds `dispatch:auto`
    in the web UI.** No trust change and very safe, but not hands-off.
    Rejected by the owner.
-   **Let the bot's labels count.** Rejected: a stolen machine-user token
    could then create trusted issues (the threat in ADR-041).
-   **Ed25519 signatures instead of HMAC**, so only the bot holds a signing
    key and the dispatcher and `/work-on` hold only a public key. Better in
    principle (the HMAC key can sign wherever it can verify), but Python's
    standard library has no Ed25519, so it needs a new, compiled dependency
    (`cryptography` or PyNaCl). Not chosen now; decision 7 limits where the
    key lives instead. Worth revisiting if the key spreads.
-   **A Discord HTTP interactions endpoint** (signed requests). It would
    need an inbound port on this PC. Rejected: outbound only.

## Consequences

-   An issue typed in Discord reaches the dispatcher without the owner
    touching GitHub.
-   A stolen machine-user token can still create and edit issues but can't
    produce a valid proof; editing a bot issue's title or body, or copying
    them to another issue, breaks the proof (this also covers #216 for
    bot-made issues).
-   Someone who has only the Discord bot token can't make Discord deliver
    an interaction as the owner.
-   One more service, one more dependency, three more credentials.
-   New work: `dispatch/proof.py` and its tests, the trust-check change,
    the bot, a systemd unit, the `/work-on` change, docs/05 §2.

## Limits

-   The proof shows that Discord said the owner sent it, not that the
    owner was at the keyboard. A takeover of the owner's Discord account
    (use two-factor authentication, keep the server private with only the
    owner in it), or of this PC (both env files), defeats it.
-   The bot process holds `PROOF_KEY` and both tokens, and the dispatcher
    and `verify` hold `PROOF_KEY` too. Anyone who runs code as the owner's
    user on this PC can read `dispatch.env` or `bot.env` and forge a proof;
    the Claude Code deny rule (decision 7) stops the session reading them,
    not other programs.
-   `verify` is as trustworthy as the clone it runs from: only merged,
    reviewed code.
-   Comments and later edits to a bot issue's labels are not covered; the
    dispatcher ignores comments, and bot labels carry no trust.
-   Discord is not end-to-end encrypted, and Discord can read task text.
