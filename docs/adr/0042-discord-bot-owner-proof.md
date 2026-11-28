# ADR-042: Discord bot (step 4d) and the owner proof

**Status:** Proposed (owner decisions 2026-11-28 on #228; draft for acceptance)\
**Date:** 2026-11-28\
**Owner:** BoxDelta\
**Amends (once accepted):** ADR-030 §Trust (a second way for an issue to
count as the owner's) and ADR-041 decision 4 (the bot's labels and issues
never count, except with a valid proof)

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
    text: the server is the owner's private server; the channel is
    `#aether-dispatch`; `interaction.user.id` equals the owner's numeric
    Discord ID (set in the bot's env file, compared as numbers); the user
    is not a bot or webhook. The command is also restricted in Discord's
    own permissions, as a second layer.
4.  **Route: `claude` only at first.** Every bot issue is a Claude-path
    issue (`dispatch:claude-only`). A local route, and dropping the
    `dispatch:local-ok` requirement, come later, after about ten clean
    local pull requests, in a new decision. Until then a valid proof can
    never stand in for `dispatch:local-ok`.
5.  **The owner proof.** The bot ends the issue body with one line:
    `<!-- aether-proof v1 ts nonce owner-id interaction-id route mac -->`.
    `mac` is HMAC-SHA256, with the key `PROOF_KEY` (32 random bytes), over
    a canonical form: a version string, owner ID, interaction ID, route,
    timestamp, nonce, then the exact title, details and files, each with
    its length in front so no field can run into the next.
6.  **The dispatcher accepts a bot-made issue as the owner's only if**
    all of these hold, each checked again before every step:
    -   the author is exactly `boxdelta-dispatch`;
    -   the proof line is the last line and the `mac` verifies, with a
        constant-time compare, over the title and body as they are now
        (so an edit to either, by anyone, fails);
    -   the owner ID in the proof is the configured owner ID, the route
        is `claude`, and the timestamp is not in the future and not
        older than 7 days (after that, the owner labels it by hand);
    -   the interaction ID has not been used before (the dispatcher keeps
        the used IDs in its state file), so a proof can't be copied onto
        a second issue.

    For such an issue the proof stands in for `dispatch:auto` and
    `dispatch:claude-only`. The labels the bot also adds are only there so
    the dispatcher's label query finds the issue; their actor is ignored.
    The existing rules stay: a `dispatch:local-ok` the bot adds never
    counts, and an issue without a valid proof that the bot opened is
    skipped.
7.  **`/work-on` uses the same check.** The command (a follow-up PR; it
    is on the ask list) accepts an issue opened by `boxdelta-dispatch`
    only when `python -m dispatch verify <n>` says the proof is valid,
    and keeps refusing every other author. The command reads the key from
    the dispatcher's env file inside that process; Claude never sees it.
    The verify call does not use up the interaction ID.
8.  **Credentials**, all outside the repository, mode 600, each revocable
    on its own:
    -   `DISCORD_BOT_TOKEN`, in the bot's env file only;
    -   a second classic `public_repo` token of `boxdelta-dispatch`, used
        only by the bot to create issues and add labels, in the bot's env
        file only;
    -   `PROOF_KEY`, in the bot's env file (to sign) and the dispatcher's
        env file (to verify). The dispatcher holds neither the Discord
        token nor the bot's GitHub token.
9.  **Privacy.** Replies are visible only to the owner and carry the issue
    number only (`#<n> created`). Logs hold IDs and counts, never task
    text. Task text goes to Discord and is not end-to-end encrypted, so
    the Telegram rules of ADR-027 apply: no memory data, secrets or
    personal records. Limits: 5 tasks per hour, title up to 120
    characters, details up to 2000.
10. **Dependency.** `discord.py`, in a `dispatch` dependency group only,
    pinned to an exact version, covered by Dependabot (the existing `uv`
    entry reads `pyproject.toml`; the one-week cooldown applies). The
    docs/03 §8 checklist is answered below.

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
-   Runs on this PC: pure Python with `aiohttp`; checked with Python 3.12
    when it is pinned.
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
-   **A Discord HTTP interactions endpoint** (signed requests). It would
    need an inbound port on this PC. Rejected: outbound only.

## Consequences

-   An issue typed in Discord reaches the dispatcher without the owner
    touching GitHub.
-   A stolen machine-user token can still create and edit issues but can't
    produce a valid proof, and editing a bot issue's title or body breaks
    its proof (this also covers #216 for bot-made issues).
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
-   The bot process holds `PROOF_KEY` and both tokens. Anyone who runs
    code as the owner's user on this PC can forge a proof.
-   Comments and later edits to a bot issue's labels are not covered; the
    dispatcher ignores comments, and bot labels carry no trust.
-   Discord is not end-to-end encrypted, and Discord can read task text.
