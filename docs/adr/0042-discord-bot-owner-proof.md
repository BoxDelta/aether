# ADR-042: Discord bot (step 4d) and the owner signature

**Status:** Proposed (owner decisions 2026-11-28 on #228; draft for acceptance)\
**Date:** 2026-11-28\
**Owner:** BoxDelta\
**Amends (once accepted):** ADR-030 §Trust (a second way for an issue to
count as the owner's), §Credentials (the Discord token and the bot's GitHub
token live in the bot's own files, owned by another Linux user) and §Local
path (the dispatcher never adds `dispatch:auto`; the bot, a separate
program, does); ADR-041 decision 4; ADR-022 decision 6 and `CLAUDE.md`
rule 12 (agents act only on issues the owner wrote). The `CLAUDE.md`
change is in the pull request that proposes this ADR, and applies only once
this ADR is Accepted and `verify` exists; docs/05 §10.1 and
`CONTRIBUTING.md` (which state the rule the old way) change in the
acceptance PR.

## Context

Step 4d of the dispatcher (ADR-030) is a Discord bot: the owner types a task
in `#aether-dispatch`, the bot creates a labelled issue, and the dispatcher
picks it up. The bot posts as the machine user `boxdelta-dispatch`
(ADR-041), and under ADR-041 nothing that account does counts as the
owner's: the issue must be opened by `BoxDelta`, and the trust labels must
be added by `BoxDelta`. So an issue made by the bot would never be picked
up, and if it were, a stolen machine-user token could create trusted
issues. The bot needs a way to show that a request came from the owner that
a stolen GitHub token cannot copy, and that a Claude Code session or the
dispatcher cannot produce either. It does not raise trust against a takeover
of the owner's own user: anything running as the owner can already make
trusted issues with the owner's admin `gh` credentials (ADR-041 Limits).

## Decision

1.  **A separate process, outbound only.** The bot is `python -m dispatch bot`.
    It uses `discord.py` over Discord's gateway (one outbound websocket) and
    HTTPS calls to `discord.com` and `api.github.com`. **It opens no
    listening port**: no HTTP interactions endpoint, no webhook receiver, no
    local API. The runbook checks this with `ss -ltnp`. Core never imports it.
2.  **A separate Linux user.** The bot runs as the system user `aether-bot`,
    from a system-level systemd unit (`aether-bot.service`), not from the
    owner's user session. Its private signing key, `DISCORD_BOT_TOKEN`, its
    GitHub token and its state are readable only by `aether-bot`. The code it
    runs is a root-owned checkout of reviewed `main` under `/opt/aether-bot`,
    which the owner's user cannot write, so, by file permissions alone, nothing
    running as the owner (Claude Code, the dispatcher, a prompt-injected
    session) can read the key or change the code that holds it. **That holds
    only if the owner's user is not root-equivalent, and today it is** (the
    `docker` group; an accepted risk for the `claude` route, see Limits). The unit is hardened (see the appendix, which also holds
    the one-time setup).
3.  **Input: one slash command, `/task`.** Options: `title`, `details`,
    `files` (optional, up to 5). Slash commands only, so the bot needs no
    Message Content intent and parses no free text. Discord delivers the
    command over the bot's authenticated gateway connection with the user ID,
    channel and server set by Discord, not by the message text.
4.  **Who is accepted.** The bot acts only if all of these hold, and
    otherwise ignores the request without replying and without logging its
    text: the numeric `guild_id` and `channel_id` equal the values in the
    bot's env file (names are never compared); `interaction.user.id` equals
    the owner's numeric Discord ID in that file; the user is not a bot or
    webhook. DMs and any interaction without a server are refused. The
    command is registered for server context only (no user-install or DM
    contexts) and restricted in Discord's own permissions. "Public Bot" is
    off, so nobody else can invite it, and the gateway intents are `guilds`
    only (no members, presence or message content).
5.  **Route: `claude` only at first.** Every bot issue is a Claude-path
    issue (`dispatch:claude-only`). A local route, and dropping the
    `dispatch:local-ok` requirement, come later, after about ten clean local
    pull requests, in a new decision. **Hard prerequisite for enabling the
    Discord local route: #230 is done** (rootless Docker or Podman, and the
    owner out of the `docker` group), because a local-route task makes the
    local model and Aider write code from owner-signed text. Until then a valid signature can never
    stand in for `dispatch:local-ok`.
6.  **The owner signature (Ed25519), bound to the issue.** The bot creates
    the issue, then edits its body once to end with one line:
    `<!-- aether-proof v2 ts nonce sig -->`. `sig` is an Ed25519 signature
    (64 bytes, base64url without padding, 86 characters) made with the
    private key that only `aether-bot` can read. The signed message is the
    domain string `aether-proof-v2`, then these fields in order, each as
    UTF-8 bytes with a fixed-width 8-byte big-endian length in front:
    repository (`owner/name`), issue number (decimal ASCII, no leading
    zeros), the owner's Discord ID (decimal ASCII), route (`claude`), `ts`
    (integer Unix seconds), `nonce` (16 random bytes as 32 lowercase hex
    characters), the title, and the body text before the proof line, taken as
    one field and never re-parsed. The repository string is a constant in
    the code, never read from the API or the environment. The body is that text, one LF, then the
    proof line, and nothing after it; an empty or null body fails. The bot
    strips leading and trailing whitespace from the title before signing
    (GitHub does too) and refuses control characters. The repository, issue
    number and owner ID are not in the visible line (the verifier supplies
    them), so the public issue does not show the owner's Discord ID. The bot
    writes LF line endings and refuses a title, details or files containing
    `<!--`, `-->`, `aether-proof` or a carriage return. Test vectors are in
    the tests for `dispatch/proof.py`; any mismatch, including from GitHub
    changing line endings, fails closed.
7.  **Verifiers hold only the public key.** The dispatcher, `/work-on`'s
    `python -m dispatch verify <n>` and the tests read the public key from
    `/etc/aether-dispatch/proof.pub` and the owner's Discord ID from
    `/etc/aether-dispatch/owner_id`. Both are **owned by root, mode 644**, in
    a root-owned directory, at fixed paths that no environment variable can
    change, so nothing running as the owner's user can swap in its own key.
    Having the public key lets a program check a signature and never make
    one.
8.  **The dispatcher accepts a bot-made issue as the owner's only if** all of
    these hold, each checked again before every step, and the brief is built
    from the same fetch that was verified:
    -   the author is exactly `boxdelta-dispatch`, and the item is an issue
        (no `pull_request` key; issues and pull requests share numbers);
    -   the proof line is the last line and the signature verifies, for this
        repository and this issue number and the title and body as they are
        now. An edit to the title or body by anyone fails, and so does
        copying the title and body onto another issue, because the issue
        number differs; no used-ID state is needed;
    -   the route is `claude`, and the timestamp is not more than 5 minutes
        in the future and not older than 7 days. After that the owner opens
        a new issue: a bot-opened issue never passes the label path, so
        adding labels to it by hand does nothing.

    For such an issue the signature stands in for `dispatch:auto` and
    `dispatch:claude-only`. The labels the bot also adds (only after the
    proof edit, so an unproven issue never shows up in the label query) are
    there so the dispatcher's query finds the issue; their actor is ignored.
    The existing rules stay: a `dispatch:local-ok` the bot adds never
    counts, and a bot-opened issue without a valid signature is skipped. The
    bot keeps the Discord interaction IDs it has served in its own state and
    creates one issue per interaction.
9.  **`/work-on` verifies with the public key** (a follow-up PR; the command
    is on the ask list). It accepts an issue opened by `boxdelta-dispatch`
    only when `verify` succeeds, and keeps refusing every other author.
    `verify` runs from the main-only clone (`~/Projects/aether-dispatch`),
    and **prints the verified title and body**; `/work-on` uses only that
    text, so an edit between the check and the read changes nothing. Once
    #230 is done, no sandbox or read-deny rule is needed to protect the
    signing key, because the owner's user can never reach it. Until then the
    key is protected by file permissions only against a session that cannot
    use Docker (the accepted risk in Limits). A `Read` deny rule or the Claude Code
    sandbox on `~/.config/aether-dispatch/**` (it holds the dispatcher's
    GitHub token) stays optional extra hardening. `verify` guards against
    text an attacker put in an issue; it cannot force a session that ignores
    the rule to run it.
10. **Credentials**, all outside the repository, each revocable on its own:
    -   in `/etc/aether-bot/bot.env`, owned by `aether-bot`, mode 600:
        `DISCORD_BOT_TOKEN`, a second classic `public_repo` token of
        `boxdelta-dispatch` (used only by the bot), and the owner, server
        and channel IDs;
    -   in `/etc/aether-bot/proof.key`, owned by `aether-bot`, mode 600: the
        Ed25519 private key;
    -   in `/etc/aether-dispatch/` (root-owned, world-readable): the public
        key and the owner's ID, which are not secrets.
    The dispatcher holds neither the Discord token, the bot's GitHub token
    nor the private key. The bot refuses to start if a file is not mode 600
    or not owned by `aether-bot`, or is a symlink. To rotate the key: make a
    new key, replace `proof.pub` with `sudo`, and re-send open tasks (proofs
    in flight fail). The bot's GitHub client has an allowlist: create an
    issue, edit that issue's body once, add the two labels; anything else is
    refused in code and a test checks it. **That binds only the honest
    bot**: a stolen bot token is a full `public_repo` token of the machine
    user and can edit any issue body (the open #216 hole), close issues,
    comment and change labels. It still cannot sign, so trust holds. The
    runbook covers revoking each token separately.
11. **Privacy and logs.** Replies are visible only to the owner and carry the
    issue number only (`#<n> created`). **The task text becomes a public
    GitHub issue** (ADR-022), readable and kept in history by anyone, so the
    docs/05 §10.4 rules apply: no personal details, no secrets, no memory
    data. The command's help text says so. Task text also passes through
    Discord, which is not end-to-end encrypted (the Telegram rules of ADR-027
    apply). Logs hold IDs and counts, never task text, and never the
    signature or the proof line; the `discord.gateway` and `discord.http`
    loggers are held at INFO or above, and the Discord token and the bot's
    GitHub token are registered with a redactor that is part of the
    `dispatch/` package. The bot imports nothing outside `dispatch/`, the
    standard library and its pinned dependency groups, so the update diff
    (appendix) covers all the first-party code that runs in the key process
    (the pinned libraries come from the `uv.lock` part of that diff); a test
    checks the imports. Limits: 5 tasks per
    hour, title up to 120 characters, details up to 2000.
12. **Dependencies, in `dispatch`-only dependency groups**, each pinned to an
    exact version, covered by Dependabot (the existing `uv` entry reads
    `pyproject.toml`; the one-week cooldown applies) and by
    `pip-audit --all-groups` in Sentinel:
    -   group `dispatch`: `cryptography` (signature check in the dispatcher,
        `verify` and the bot, and the tests);
    -   group `dispatch-bot`: `discord.py`, installed only in the bot's
        environment, so the dispatcher, which runs with the owner's
        credentials, never installs the gateway library.

    Sentinel's `uv sync --locked` installs only default groups, and
    `mypy` would fail on the missing import first. So the pull request that
    adds the groups also changes the Sentinel step to `--group dispatch` (a
    `.github/` edit, with the owner's approval), and the proof tests must
    **fail, not skip**, when `cryptography` is missing (docs/05 §10: a skipped
    check must be visible). `discord.py` is imported only in a thin layer of
    the bot, lazily; the rest of the bot is tested in Sentinel against a fake
    gateway. The trust check imports `cryptography` lazily and treats an
    import failure as "no valid signature" (bot issues skipped, every other
    path unaffected); a test covers it.

### docs/03 §8 checklist for `cryptography`

-   Actively maintained: yes (pyca), regular releases; the release date is
    checked when the version is pinned.
-   License: Apache-2.0 or BSD-3-Clause.
-   API fit: Ed25519 key loading, signing and verification.
-   Data leaving the machine: none.
-   Self-hosted: a local library.
-   Costs and limits: free.
-   Replaceable: yes. The signature is standard Ed25519; any library can
    verify it (PyNaCl, for one).
-   Runs on this PC: compiled (Rust and OpenSSL, shipped as wheels). Check
    that a wheel exists for the project's Python (`.python-version`, 3.14)
    when pinned; it is not in `uv.lock` yet.
-   New privileged execution path: no. It is a library with no network or
    file access of its own.
-   Failure isolation: a requirement, with a test (decision 12): if it fails
    to import, the dispatcher skips bot issues (fails closed) and every other
    path is unaffected.

### docs/03 §8 checklist for `discord.py`

-   Actively maintained: yes, version 2.x with regular releases; the
    release date is checked when the version is pinned.
-   License: MIT.
-   API fit: Discord gateway and application commands (slash commands).
-   Data leaving the machine: the owner's `/task` text and the bot's replies
    go through Discord; the bot token authenticates the bot. No memory data,
    secrets or personal records (decision 11).
-   Self-hosted: Discord is a hosted service; the bot runs on this PC.
-   Costs and limits: free. Discord's rate limits are far above 5 tasks an
    hour.
-   Replaceable: yes. The signature and the issue creation do not depend on
    the library; another client could call them.
-   Runs on this PC: `discord.py` is pure Python but brings `aiohttp` and its
    transitive packages (for example `multidict`, `yarl`, `frozenlist`,
    `propcache`, `aiohappyeyeballs`, `aiosignal`, `attrs`, `idna`, and
    `audioop-lts` on Python 3.13 and later), some with compiled parts, none in `uv.lock` yet. When the version is
    pinned, list them with their licences, and check the lot against the
    project's Python (`.python-version`, 3.14) including any backport
    package it asks for.
-   New privileged execution path: **yes**. The bot can create issues the
    dispatcher trusts. Decisions 2, 4, 6 and 8 limit that. It has no merge
    call, no Core access and no memory access.
-   Failure isolation: its own process, user and unit, in a separate
    dependency group; Core and the dispatcher's timer do not import it.

## Alternatives considered

-   **HMAC with a shared key** (the first draft). Rejected by the owner:
    anything that can verify can also sign, so the key would have to stay
    out of reach of Claude sessions with a deny rule or sandbox, and a `Read`
    deny rule does not stop Bash.
-   **The bot makes an unlabelled issue and the owner adds `dispatch:auto`
    in the web UI.** No trust change and very safe, but not hands-off.
    Rejected by the owner.
-   **Let the bot's labels count.** Rejected: a stolen machine-user token
    could then create trusted issues (the threat in ADR-041).
-   **The bot as a systemd user unit under the owner's account.** Rejected:
    the signing key would be readable by anything running as the owner.
-   **A Discord HTTP interactions endpoint** (signed requests). It would need
    an inbound port on this PC. Rejected: outbound only.

## Consequences

-   An issue typed in Discord reaches the dispatcher without the owner
    touching GitHub.
-   A stolen machine-user token can still create and edit issues but can't
    sign; editing a bot issue's title or body, or copying them to another
    issue, breaks the signature (this also covers #216 for bot-made issues).
-   Someone who has only the Discord bot token can't make Discord deliver an
    interaction as the owner.
-   Claude Code and the dispatcher hold only the public key, so a
    prompt-injected session cannot forge a signature, unless it can use
    Docker (the accepted risk in Limits, until #230).
-   One more Linux user, system unit, service, dependency pair and set of
    credentials, and an update step (below) when the bot's code changes.
-   New work: `dispatch/proof.py` and its tests, the trust-check change, the
    bot, the unit file, the `/work-on` change, docs/05 §2.

## Limits

-   The signature shows that Discord said the owner sent it, not that the
    owner was at the keyboard. A takeover of the owner's Discord account (use
    two-factor authentication, keep the server private with only the owner
    in it) defeats it.
-   **Accepted risk: the `docker` group (owner decision 2026-11-28).** The
    owner's user is in the `docker` group (checked 2026-11-28), which is
    effectively root: a container can read `/etc/aether-bot/proof.key`,
    replace `/etc/aether-dispatch/proof.pub`, or rewrite `/opt/aether-bot`,
    with no password and no Claude Code prompt (the dispatcher's local path
    and the Postgres backup already use `docker` as the owner, ADR-025). So
    the user boundary does **not** stop anything running as the owner that
    can use Docker (Claude Code, the dispatcher, a prompt-injected session).
    The owner accepts this while the Discord bot is **claude-route only**:
    a forged signature then yields only a Claude brief that the owner reads,
    and `/work-on` stays a supervised session. Moving to rootless
    Docker/Podman is tracked in #230. The other options (a key on a hardware
    token, or a prompt on `docker` commands) were not chosen; the optional
    `Bash(docker *)` ask rule is not added. **#230 must be done before the
    Discord local route is enabled** (decision 5). The runbook
    checks the group so the state stays visible.
-   Root can read the key, and so can anyone who gets root on this PC or
    `aether-bot`'s files by another route. The owner's own sudo is the
    boundary, and only while nothing has written to the owner's shell
    configuration (a `sudo` function or a PATH entry would capture the
    password): the boundary is the owner's whole login environment (shell
    configuration, `PATH`, `environment.d`). Run the runbook from a text
    console or `/usr/bin/fish --no-config` with `/usr/bin/sudo` and absolute
    paths, as written. `pkexec`, `run0`, `systemd-run`,
    `systemctl` and `su` are not on the Claude Code ask list; adding them is
    a follow-up (`.claude/settings.json`, owner approval). GNOME can show a
    polkit password dialog for them.
-   The private key lives in the same process as `discord.py` and `aiohttp`,
    which parse network input from Discord. The separate dependency group
    protects the dispatcher, not the key. A small signing process with no
    network access, called by the bot, would fix that; not chosen now.
-   The bot's code is only as good as the commit the owner deploys. The
    update step shows the commit before it is deployed.
-   Comments and later edits to a bot issue's labels are not covered; the
    dispatcher ignores comments, and bot labels carry no trust.
-   Discord is not end-to-end encrypted, and Discord can read task text.
-   The separate Linux user raises no trust against a takeover of the owner's
    own user (the context above).
-   `/work-on` is a rule a session follows. The signature stops forgery, not
    a session that skips the check.

## Appendix: runbook (the owner runs this with sudo; fish)

Run it from a clean login: best from a text console (Ctrl+Alt+F3), or at
least from `/usr/bin/fish --no-config`. Your normal shell and login
environment (fish configuration, `PATH`, `~/.config/environment.d/`) are
where something running as you could hide a fake `sudo` or `fish`, or an
`LD_PRELOAD`. Call `/usr/bin/sudo` with absolute paths for every tool run
under sudo, because `sudo` must not run a file your user can write. The
commands below do that.

The commands in part 1 can be run before the bot exists. Part 2 is fixed in
the pull request that adds the bot, which also adds the unit file.

**Part 1: user, directories, key.**

```fish
/usr/bin/sudo /usr/bin/useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/bin/nologin aether-bot
/usr/bin/sudo /usr/bin/install -d -o aether-bot -g aether-bot -m 700 /etc/aether-bot /var/lib/aether-bot
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 755 /etc/aether-dispatch
/usr/bin/sudo -u aether-bot /usr/bin/sh -c 'umask 077; /usr/bin/openssl genpkey -algorithm ed25519 -out /etc/aether-bot/proof.key'
/usr/bin/sudo -u aether-bot /usr/bin/openssl pkey -in /etc/aether-bot/proof.key -pubout | /usr/bin/sudo /usr/bin/tee /etc/aether-dispatch/proof.pub >/dev/null
/usr/bin/sudo /usr/bin/chown root:root /etc/aether-dispatch/proof.pub
/usr/bin/sudo /usr/bin/chmod 644 /etc/aether-dispatch/proof.pub
printf '%s\n' YOUR_NUMERIC_DISCORD_ID | /usr/bin/sudo /usr/bin/tee /etc/aether-dispatch/owner_id >/dev/null
/usr/bin/sudo /usr/bin/chown root:root /etc/aether-dispatch/owner_id
/usr/bin/sudo /usr/bin/chmod 644 /etc/aether-dispatch/owner_id
/usr/bin/sudo /usr/bin/install -o aether-bot -g aether-bot -m 600 /dev/null /etc/aether-bot/bot.env
printf 'BOT_OWNER_DISCORD_ID=%s\nBOT_GUILD_ID=%s\nBOT_CHANNEL_ID=%s\n' YOUR_ID SERVER_ID CHANNEL_ID | /usr/bin/sudo -u aether-bot /usr/bin/tee -a /etc/aether-bot/bot.env >/dev/null
read -s -P 'Discord bot token: ' tok; and printf 'DISCORD_BOT_TOKEN=%s\n' $tok | /usr/bin/sudo -u aether-bot /usr/bin/tee -a /etc/aether-bot/bot.env >/dev/null; set -e tok
read -s -P 'second boxdelta-dispatch token (public_repo, 90 days): ' tok; and printf 'BOT_GITHUB_TOKEN=%s\n' $tok | /usr/bin/sudo -u aether-bot /usr/bin/tee -a /etc/aether-bot/bot.env >/dev/null; set -e tok
```

The variable names are fixed in the bot pull request. Replace the capitals
with the real numbers. The Discord bot is created in the Discord developer
portal with "Public Bot" off and no privileged intents.

**Part 1 checks** (each must behave as noted):

```fish
/usr/bin/cat /etc/aether-bot/proof.key            # Permission denied
/usr/bin/cat /etc/aether-bot/bot.env              # Permission denied
/usr/bin/sudo -u aether-bot /usr/bin/cat ~/.config/aether-dispatch/dispatch.env   # Permission denied
/usr/bin/sudo -k; /usr/bin/sudo -n /usr/bin/true; echo $status   # non-zero: sudo asks for a password
/usr/bin/sudo -l | /usr/bin/grep -i nopasswd      # no output
id -nG | string match -q '*docker*'; and echo "owner is root-equivalent via Docker (see Limits)"
/usr/bin/ls -l /etc/aether-dispatch               # both files root:root, -rw-r--r--
```

**Part 2: deploy the code and the unit** (after the bot pull request is
merged; the exact commands and the unit file come with it).

```fish
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 755 /opt/aether-bot
/usr/bin/sudo /usr/bin/git clone https://github.com/BoxDelta/aether.git /opt/aether-bot/repo
/usr/bin/sudo /usr/bin/env UV_PROJECT_ENVIRONMENT=/opt/aether-bot/venv UV_PYTHON_INSTALL_DIR=/opt/aether-bot/python /usr/bin/uv sync --locked --no-build --no-dev --group dispatch --group dispatch-bot --project /opt/aether-bot/repo
/usr/bin/sudo /usr/bin/install -o root -g root -m 644 /opt/aether-bot/repo/scripts/systemd/system/aether-bot.service /etc/systemd/system/aether-bot.service
/usr/bin/sudo /usr/bin/systemctl daemon-reload
/usr/bin/sudo /usr/bin/systemctl enable --now aether-bot.service
```

`uv` is the pacman copy at `/usr/bin/uv` (root-owned). The `--no-build` flag
installs wheels only, so no package build runs as root, and
`UV_PYTHON_INSTALL_DIR` keeps the interpreter where `aether-bot` can run it.

The unit runs as `aether-bot` with `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=true`, `ProtectProc=invisible`, `ProcSubset=pid`, `PrivateTmp`,
`PrivateDevices`, `ProtectKernel*`, `RestrictNamespaces`, `RestrictSUIDSGID`,
`LockPersonality`, `UMask=0077`, an empty `CapabilityBoundingSet`,
`RestrictAddressFamilies=AF_INET AF_INET6`, a system-call filter, and write
access only to `/var/lib/aether-bot`. For the network it uses
`IPAddressDeny=localhost link-local multicast 10.0.0.0/8 172.16.0.0/12
192.168.0.0/16 100.64.0.0/10 fc00::/7` with `IPAddressAllow=` for only the
DNS resolver. That keeps a compromised dependency away from the loopback
services (LiteLLM, Postgres, n8n), Docker's bridge networks, the LAN and
Tailscale; it can still reach the public internet, which it needs for Discord
and GitHub, and that includes this PC's own public addresses. So every
local service must listen on `127.0.0.1` only (docker-compose publishes with
`127.0.0.1:`). Check before starting the bot:
`/usr/bin/ss -ltn | /usr/bin/grep -E '(0\.0\.0\.0|\[::\]|\*):'` should list
nothing you did not intend to expose; fix any hit first.

**Updating the bot.** Everything merged to `main` reaches the process that
holds the key, so read the whole diff of the bot's code, dependencies and
unit before deploying (a Dependabot bump of `discord.py` or `aiohttp` counts):

```fish
/usr/bin/sudo /usr/bin/git -C /opt/aether-bot/repo fetch origin main
/usr/bin/sudo /usr/bin/git -C /opt/aether-bot/repo log --oneline HEAD..origin/main
/usr/bin/sudo /usr/bin/git -C /opt/aether-bot/repo diff --stat HEAD origin/main
/usr/bin/sudo /usr/bin/git -C /opt/aether-bot/repo diff HEAD origin/main -- dispatch scripts/systemd pyproject.toml .python-version uv.lock
/usr/bin/sudo /usr/bin/git -C /opt/aether-bot/repo merge --ff-only origin/main
/usr/bin/sudo /usr/bin/env UV_PROJECT_ENVIRONMENT=/opt/aether-bot/venv UV_PYTHON_INSTALL_DIR=/opt/aether-bot/python /usr/bin/uv sync --locked --no-build --no-dev --group dispatch --group dispatch-bot --project /opt/aether-bot/repo
/usr/bin/sudo /usr/bin/install -o root -g root -m 644 /opt/aether-bot/repo/scripts/systemd/system/aether-bot.service /etc/systemd/system/aether-bot.service
/usr/bin/sudo /usr/bin/systemctl daemon-reload
/usr/bin/sudo /usr/bin/systemctl restart aether-bot.service
```

The diff includes `uv.lock` in full, so a bumped transitive package
(`aiohttp`, `yarl`, `multidict`, ...) shows as a changed version line. The
bot imports nothing outside `dispatch/` (decision 11), so the diff is all the
first-party code in the key process plus the exact library versions it will
run.

**Checks after starting:** `/usr/bin/ss -ltnp` shows nothing for `aether-bot`;
`systemd-analyze security aether-bot.service` shows the hardening; a `/task`
from another Discord user is ignored.

**Revoking.** Delete the bot's GitHub token in `boxdelta-dispatch`'s
settings, or reset the Discord bot token in the developer portal; each is
independent. To stop signing at once,
`/usr/bin/sudo /usr/bin/systemctl disable --now aether-bot.service`. To
invalidate every earlier signature, replace `/etc/aether-dispatch/proof.pub`
with a new public key.
