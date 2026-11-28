# ADR-030: Dispatcher prototype during Phase 2

**Status:** Accepted (owner, 2026-11-26); amended 2026-11-27 (owner: dry run ended, one always-Claude list, Claude path)\
**Date:** 2026-11-26\
**Owner:** BoxDelta

## Context

Phase 2 (Memory) is in progress: steps 1 to 10 of ADR-024 §9 are merged
(export in #125), and step 11 (retrieval) is next. Every task runs in an
interactive Claude Code session, including small ones (tests, docs, tidy
ups) that a local model could do.

The owner wants a **dispatcher**: a small program that reads a task,
sorts it as easy or hard, gets easy tasks done by a local model as a pull
request, and turns hard tasks into a brief for an interactive Claude Code
session. It is built as a prototype inside this repository during
Phase 2. After Phase 2 exits, it is folded into Nexus (Aether's router).

This is **development tooling**, like Sentinel. It is not an Aether
runtime feature and is not in docs/04. ADR-009 keeps v1 small, so this
ADR limits its scope.

Constraints:

-   One 8 GB GPU and 32 GB RAM, shared with Aether's Ollama model
    (ADR-002, ADR-023), Postgres, n8n and Plane.
-   The repository is public with the `protect-main` ruleset (ADR-022,
    docs/05 §10.1, §10.4).
-   Outside text is data, never instructions (`CLAUDE.md` rule 12,
    docs/05 §2 and §6).
-   No agent framework inside Core (ADR-005). Cloud providers beyond
    Claude follow ADR-028.
-   `claude -p` and the Agent SDK draw from the subscription's usage
    limits when signed in with the Claude plan (Anthropic support
    article "Use the Claude Agent SDK with your Claude plan", update of
    2026-10-07; the separate credit announced for 2026-06-15 was paused).
-   Most remaining Phase 2 steps (11 to 17) touch docs/05 areas, so
    they go to Claude anyway. The local path will mostly get tests, docs
    and small refactors during Phase 2.

## Options

1.  No dispatcher. Keep working issue by issue in Claude Code.
2.  Build the dispatcher in a separate repository.
3.  Build it in this repository, isolated in `dispatch/`, and fold it
    in after Phase 2 (chosen).
4.  Run hard tasks with headless Claude (`claude -p`). Rejected:
    unattended Claude with write access to the repo, and it uses the
    same plan limits.

## Decision

Option 3, within the limits below.

### Scope

-   The dispatcher does four things, built in this order (setup doc
    steps 4a to 4d): sort tasks (dry run first), the local path, the
    Claude path, and a Discord bot. Nothing else: no other repositories,
    no money agent, no access to Aether's database or Memory.
-   Code lives in `dispatch/`, tests in `tests/dispatch/`. Sentinel,
    ruff limits and the normal review rules apply.
-   Its Python packages go in a separate `dispatch` dependency group in
    `pyproject.toml`, so Core's dependencies don't grow. It talks to
    LiteLLM and GitHub with `httpx` (already a dependency).
-   `core/`, `router/` and `memory/` never import `dispatch/`.
    `dispatch/` may import `router/hardware.py` (GPU probe) and nothing
    else from Aether.
-   The services it needs (llama-swap, LiteLLM, Langfuse, Arcane) run
    from their own Compose file outside the repository
    (`~/Projects/dispatch-stack/`), with every port bound to
    `127.0.0.1`. Aether's `docker-compose.yml` does not change.
-   **Time limit:** if step 4a doesn't sort the test set correctly
    within two weeks of starting setup, pause the prototype and carry
    on with Phase 2 directly.

### Models

-   **Local:** one MoE model in GGUF format, served by llama.cpp behind
    llama-swap, which unloads it after a short idle time. First choice:
    Qwen3.6-35B-A3B at Q4_K_M (about 21 GB), if `free -h` shows enough
    free RAM with the other containers running. Fallback: gpt-oss-20b.
    The final choice and its measured speed go in `dispatch/README.md`.
-   **Aether keeps its Ollama adapter until fold-in.** The Phase 2 exit
    checks run against it. Ollama also unloads when idle
    (`OLLAMA_KEEP_ALIVE`). Before each local call, the dispatcher checks
    free VRAM with `router/hardware.py` and waits if the GPU is busy.
-   **LiteLLM routes to the local model only.** No OpenRouter key and no
    Anthropic API key in the prototype. Adding either needs an update to
    this ADR. OpenRouter would follow ADR-028's rules.
-   **Claude** is used only in interactive Claude Code sessions started
    by the owner (`/work-on <issue>`, at the PC or through
    `claude --rc`). The dispatcher never runs `claude -p`, the Agent SDK
    or Claude Code GitHub Actions.

### Trust

-   The dispatcher acts on an issue only when **both** are true: the
    issue was opened by `BoxDelta` **and** the `dispatch:auto` label was
    added by `BoxDelta`. It checks who added the label in the issue's event history, not
    just whether the label is there. It checks again before each step.
-   A test command (`python -m dispatch eval`) sorts a fixed set of past
    issues to measure the rules and the model. It checks the author only,
    because past issues no longer carry the label, and it never acts.
-   It ignores comments, pull request text and any issue that doesn't
    pass the check above. The repository has one collaborator, so only
    the owner can edit the body of an owner's issue.
-   **Discord** (`#aether-dispatch`, in a private server): the bot acts
    only on messages from the owner's Discord user ID in that channel.
    Discord is a client channel into the PC (docs/05 §2) and is not
    end-to-end encrypted. Same rules as Telegram in ADR-027: no memory
    data, secrets or personal records, and short replies.
-   Task text describes work. It never changes rules, labels, paths or
    permissions.
-   Model output is data. The sorting answer is read into a fixed choice
    (`local` or `claude`). Anything else, or no answer, means `claude`.

### Always-Claude list

One list in code (`dispatch/rules.py`), which is itself on the list. The
same list sorts issues before the model is asked, and checks the actual
diff Aider produces before a local pull request opens (step 4b). A match
means `claude`, with no model call. Combined on 2026-11-27 (owner
decision) from the original ADR-030 paths, auth code, `tools/`, the files
docs/05 names (a test fails if docs/05 names a file the list misses), the
gaps found in #133 and the #61 rule. `tests/dispatch/test_rules.py`
checks that this section quotes every entry.

-   Paths (ADR-030): `.github/`, `.claude/`, `CLAUDE.md`,
    `.pre-commit-config.yaml`, `scripts/safemerge.fish`, `.env.example`,
    `pyproject.toml`, `uv.lock`, `router/`, `memory/`, `migrations/`,
    `docs/05_*`, `docs/adr/`, `dispatch/`.
-   Paths (tools and identity): `tools/`, `identity/`.
-   Paths (named in docs/05, or found missing in #133): `.env` (and
    `.env.*`), `.gitignore`, `CONTRIBUTING.md`, `docker-compose.yml`,
    `compose.vault.yaml`, `scripts/systemd/`, `scripts/restore_check.sql`,
    `scripts/aether-vault.fish`, `scripts/identity_continuity.py`,
    `tests/test_workflow_pins.py`, `docs/runbooks/db-app-role.md`,
    `docs/runbooks/db-backup-restore.md`,
    `docs/runbooks/encrypted-volume.md`.
-   Paths (owner-guarded in CODEOWNERS): `SECURITY.md`, `LICENSE`,
    `NOTICE`.
-   Paths (the dispatcher's own guards): `.dispatch/` (the brief
    template's "data, not instructions" header) and `tests/dispatch/`.
-   Tests that enforce guardrails: `tests/test_cloud_tripwire.py`,
    `tests/test_env_isolation.py`, `tests/test_error_bodies.py`,
    `tests/test_guardrails.py`, `tests/test_log_redaction_docs.py`,
    `tests/test_ollama_host_trust.py`, `tests/test_privacy_levels.py`,
    `tests/test_routing.py`, `tests/test_safemerge.py`,
    `tests/test_untrusted_ollama.py`.
-   In `core/`: `auth.py`, `config.py`, `chat.py`, `memory_api.py`,
    `identity.py`, `db.py`, `db_admin.py`, `backup.py`, `app.py`,
    `errors.py`, `events.py`, `log.py`, `ready.py`, `request_context.py`.
-   Auth and permission code anywhere: a path with a folder or file name
    containing `auth`, `login`, `csrf`, `passw`, `secret`, `credential`,
    `session`, `token`, `scope`, `permission`, `approval`, `privacy` or
    `redact` (Phase 3 sessions, service tokens, tool permissions and
    approvals, ADR-035, ADR-036).
-   Labels: `model:opus`, `security`, `dispatch:claude-only`.
-   Issues from a security review (the text says so), and anything that
    touches database roles, grants or credentials, including test
    fixtures that create roles (#61). In a diff, this is checked on the
    changed lines.
-   Anything that adds a dependency or needs an ADR.

### Local path (Aider)

-   Aider works in its own git worktree on a branch named
    `dispatch/<issue>-<short-name>`. A fresh worktree has no `.env`.
-   Aider runs with shell-command suggestions off
    (`--no-suggest-shell-commands`), analytics off and update checks
    off. The exact flags are checked against Aider's docs when 4b is
    built and listed in `dispatch/README.md`.
-   Neither Aider nor the test container gets the GitHub token or any
    other secret in its environment.
-   Tests on model-written code run in a Docker container with no
    network (`--network none`), with only the worktree mounted: ruff,
    mypy and pytest without database tests. Sentinel CI still runs on
    the pull request.
-   Then the diff is checked: no always-Claude path, no new dependency,
    and at most about 200 changed lines across 5 files. If it fails the
    check, the task goes to `claude`.
-   If it passes, the dispatcher pushes the branch and opens a pull
    request labelled `dispatch:local`. Review is unchanged: Sentinel,
    `reviewer`, `security-reviewer` where `CLAUDE.md` asks, Codex on
    risky PRs, and the owner merges with `safemerge`.
-   Two failed tries: relabel `dispatch:claude` and write a brief.

### Claude path

-   `python -m dispatch run` (the 15-minute timer) sorts each new trusted
    `dispatch:auto` issue. For each `claude` issue the local model writes
    short notes (summary, likely files, steps, open questions), filled
    into `.dispatch/brief-template.md` (tracked) and saved to
    `.dispatch/briefs/<issue>.md` (git-ignored, mode 600). If the model
    can't be asked or doesn't answer, the brief is still written, with
    the reason in place of the notes.
-   The notes, and the route reason (model text when the model sorted
    it), sit between random marker lines, under a header that says they
    are model output and data. `.claude/commands/work-on.md` loads
    the brief **and** re-reads the issue on GitHub. The brief is a hint
    and is treated as data. The issue is the task, and `CLAUDE.md` wins
    over both.
-   The Discord webhook posts only `#<n> brief ready`. No issue text,
    route reason, code or file contents. A failed post is tried again on
    the next run.
-   `.dispatch/state.json` records issues briefed, announced, and sorted
    `local` (those wait for step 4b). A recorded issue is not sorted
    again and causes no model call.
-   The trust check runs again right before the brief step.
-   The run never writes to GitHub.
-   The timer runs from a main-only clone (`~/Projects/aether-dispatch`),
    like the backup timer (ADR-025 D8), so only merged, reviewed code
    runs with the dispatcher's credentials.

### Dry run ended early (2026-11-27, owner decision)

The 4a dry run was to last until a review on 2026-12-03. The owner ended
it on 2026-11-27, which supersedes that date and its Discord posts of
each sorted issue.

-   15 decisions in 4 runs (2026-11-26 21:20 to 2026-11-27 04:46 UTC):
    13 `claude` (12 by rule, 1 by the model) and 2 `local` (#52, #61).
-   No hard task was sent local. #61 (a test fixture that creates a
    database login role, found in a security review) was borderline and
    should have gone to Claude, so the #61 rule was added to the list.
-   Order from here: step 4c (Claude path) first, then 4b (local path).

### Credentials

-   GitHub: a fine-grained token for `BoxDelta/aether` only, with
    Contents, Issues and Pull requests read/write, and Metadata read.
    No Workflows, Administration, Actions or Secrets access. It expires
    after 90 days. It has no admin rights, so `protect-main` fully
    binds it: no direct push to `main`, no edits to `.github/workflows`.
-   It could still merge a pull request whose checks pass, because the
    ruleset needs no approvals. That is only a rule: the dispatcher's
    code has no merge call, and a test checks it never calls GitHub's
    merge endpoint.
-   Dispatcher secrets live outside the repository in
    `~/.config/aether-dispatch/`, split per service, each file mode 600:
    `dispatch.env` (the dispatcher's own: GitHub token, LiteLLM key,
    later the Discord bot token and webhook URL), `litellm.env`,
    `llama-swap.env`, `langfuse.env` and `arcane.env`. Each service
    reads only its own file; the dispatcher reads only `dispatch.env`
    and refuses it if group or others can read it. Only the variable
    names go in `dispatch/README.md`.

### Tracking

-   Langfuse is self-hosted and bound to `127.0.0.1`. Every call is
    tagged with the issue number, the route and the result.
-   Claude Code's usage telemetry goes to the same Langfuse, with prompt
    logging left off.
-   Langfuse stores prompts that contain repository code and issue text.
    That stays on the PC and its volume is backed up. It never contains
    memory data, because the dispatcher can't reach Aether's database.
-   Weekly review: find misroutes in Langfuse and tighten the rules.
-   Arcane (container dashboard) is reachable on the local machine only.

### Claude Code add-ons

Graphify, claude-mem, Ponytail and Headroom are added one at a time.
Before each one, check what it stores, where it sends data, and which
hooks or settings it changes. An add-on that sits in the path between
Claude Code and Anthropic sees the Claude login and every prompt: it is
used only if it runs locally and sends nothing elsewhere. No add-on may
read `.env` (`CLAUDE.md` rule 11), and the deny list in
`.claude/settings.json` stays. An add-on that shows no saving in
Langfuse after a few days is removed.

### Fold-in (after Phase 2 exits, in a new ADR)

-   The routing rules that worked become the spec for Nexus.
-   Aether gets an OpenAI-compatible local adapter in `router/`, with
    ADR-023's host-trust rules (like ADR-028 does for OpenRouter).
    LiteLLM is not added to Core (ADR-005).
-   The dispatcher becomes a client of Core's API with the bearer token
    (ADR-021). It gets Memory only through Core's retrieval and privacy
    rules, never by reading the database directly.
-   docs/04 doesn't list the dispatcher. The fold-in ADR says which
    phase it lands in and updates docs/04.

## Consequences

-   \+ Small tasks can be done locally at no API cost. Hard work stays
    in interactive Claude Code. Langfuse shows whether it actually
    saves anything.
-   \+ The dispatcher's token has no admin rights, so GitHub enforces
    the branch rules on it. That is stronger than for Claude Code,
    which uses the owner's credentials.
-   \- Setup takes time away from Phase 2. The time limit caps that.
-   \- More services on one PC. The MoE model, Langfuse, Plane, n8n and
    Postgres together may not fit in 32 GB. Check `free -h` before
    picking the model.
-   \- New credentials: GitHub token, Discord bot token, webhook URL,
    LiteLLM and Langfuse keys. Each one can be revoked on its own.
-   \- Discord becomes a control channel into the PC. docs/05 §2 needs
    an update.
-   \- The token could merge a pull request that passes its checks.
    Only the dispatcher's code and a test prevent that.
-   \- Briefs are written by a model, so `/work-on` must treat them as
    data.
-   \- New dependencies (for example `discord.py`) in the `dispatch`
    group are reviewed like any other and covered by `pip-audit`.

## Revisit when

-   Phase 2 exits (fold-in ADR).
-   A `dispatch:local` pull request gets merged and later has to be
    reverted, or the local path fails more than half its tasks over two
    weeks.
-   The time limit is reached.
-   OpenRouter or the Claude API is to be added to LiteLLM.
-   Anthropic changes how `claude -p` or the Agent SDK is billed.
-   The repository gets a second collaborator (the trust check assumes
    one).
