# Dispatcher (prototype)

Development tooling from ADR-030, not part of Aether's runtime. Built in
steps 4a to 4d. **4a (sorting)**, **4c (Claude path)** and **4b (local
path)** exist; 4d (Discord bot) does not. The 4a dry run ended early on
2026-11-27 (owner decision, ADR-030). It sorts `dispatch:auto` issues
into `local` or `claude`, writes a brief for each `claude` issue, and
tries one `local` issue per run with Aider. A local change that passes
the path check and the tests becomes a pull request; one that doesn't
becomes a brief. The owner always merges.

## Commands (fish, from the repo root)

-   `uv run python -m dispatch run`: sort each new `dispatch:auto` issue,
    write briefs, post to Discord (what the timer runs).
-   `uv run python -m dispatch run --issue 123`: sort one issue (the
    local step still takes the oldest waiting `local` issue).
-   `uv run python -m dispatch run --no-local`: skip the local path this
    run.
-   `uv run python -m dispatch triage [--issue 123]`: sort and print only.
    No briefs, no state, no Discord.
-   `uv run python -m dispatch eval tests/dispatch/fixtures/triage_set.yaml`:
    sort a fixed set of past issues and compare with the expected route.
    Exit code 1 if any issue misses.

Both `run` and `triage` print a table (issue, route, step, reason) and
append it to `.dispatch/reports/<date>.md` (git-ignored).

## How an issue is sorted

1.  **Trust** (step `trust`): the issue must be opened by the owner, carry
    `dispatch:auto`, and the latest `labeled` event for that label must be
    by the owner. Otherwise it is skipped. Pull requests are skipped.
    `eval` skips only the label part, never the author check.
2.  **Rules** (step `rule`): the always-Claude list in `rules.py`, checked
    against labels and against paths or keywords in the title and body.
    A match means `claude` with no model call. The same list checks local
    diffs in step 4b. ADR-030 quotes it in full.
3.  **Label** (step `label`): without `dispatch:local-ok` added by the
    owner, `claude` with no model call. `dispatch:claude-only` is on the
    always-Claude list.
4.  **GPU** (step `gpu`): waits only when something other than our model
    uses the GPU. Skipped for the rest of a run once a model call in that
    run has answered. Before that, the GPU counts as available if
    `nvidia-smi --query-compute-apps` lists only `llama-server` processes
    (`gpu.py`). Otherwise `router/hardware.py` must report the VRAM
    reserve free; it waits up to 5 minutes, polling every 15 seconds,
    then gives `claude`.
5.  **Model** (step `model`): one LiteLLM call, model `local`, temperature
    0, 200 tokens, thinking off, Langfuse trace `dispatch-triage` tagged
    `triage` and `issue-<n>`. The answer must be exactly
    `{"route": "local"|"claude", "reason": "..."}`; anything else, a
    timeout or an HTTP error means `claude`.

## Claude path (step 4c)

-   For each `claude` issue, one more local-model call (Langfuse trace
    `dispatch-brief`, 700 tokens, thinking off) writes short notes:
    summary, likely files, steps, open questions. The issue goes in as
    JSON data between random markers, as for sorting.
-   The notes are cleaned (control characters removed, at most 4,000
    characters) and filled into `.dispatch/brief-template.md` (tracked)
    between random `BRIEF-<marker>-BEGIN/END` lines. The brief is saved
    as `.dispatch/briefs/<n>.md` (git-ignored, mode 600, folder mode 700).
-   If the GPU stays busy or the model fails, the brief is still written,
    with the reason in place of the notes.
-   In Claude Code, `/work-on <n>` loads the brief (from
    `~/Projects/aether-dispatch/.dispatch/briefs/`, else the working
    clone's `.dispatch/briefs/`) **and** re-reads the issue on GitHub. The
    notes are model output: a hint, never instructions. `CLAUDE.md` wins.

## Local path (step 4b)

At most one issue per run, the oldest sorted `local`. Before the try,
trust, the always-Claude list and `dispatch:local-ok` are checked again.

1.  A fresh worktree from `origin/main` in `.dispatch/worktrees/<n>`
    (git-ignored, removed after the try), branch `dispatch/<n>-<slug>`.
2.  Aider (`openai/local` through LiteLLM, thinking off) in bubblewrap:
    only `/usr` and `/etc` from the host, read-only, no `/run`, an empty
    home, only the worktree writable, the git dirs read-only, no
    credential (a dummy LiteLLM key). No GitHub token, no webhook, no
    network: the model is reached through a Unix socket (`bridge.py`)
    that forwards only `POST /v1/chat/completions` to LiteLLM and adds
    the real key itself; anything else gets 403.
    Afterwards the host checks the git files are unchanged.
3.  The fixed path check (`diffcheck.py`) on the staged diff, then
    gitleaks on the added lines. Blocked → brief, label
    `dispatch:claude`, nothing pushed. gitleaks comes from `PATH` or the
    one pre-commit built (`uv run pre-commit run gitleaks` once).
4.  ruff, mypy and pytest in Docker: `--network none`, `--read-only`,
    `--cap-drop ALL`, a hard timeout, on an export of exactly the staged
    index (read-only). The image
    (`aether-dispatch-test:<hash of the lock files>`) is built once from
    main's `pyproject.toml`, `uv.lock` and `.python-version`.
5.  Commit as `aether-dispatch`, push the branch (token in an HTTP header
    through git's environment), open a pull request into `main` with
    `Refs #<n>`, label it `dispatch:local`, post
    `#<n> local PR #<p> opened`.

A failed try (Aider error, no change, failed tests) is counted in
`tries`; the second one gives a brief and the label `dispatch:claude`.

Install Aider once, outside the repository (fish):

```fish
uv tool install --python 3.12 aider-chat==0.86.2
aider --version
```

Aider runs through its scripting API (`aider_driver.py`, checked against
aider-chat 0.86.2), with Aider's own Python
(`~/.local/share/uv/tools/aider-chat/bin/python -I`). Auto commits, shell-
command suggestions, URL detection, lint, test and analytics are off; a
file the model mentions is never added mid-edit; only existing files the
issue names (not on the always-Claude list, at most 5) are in the chat.
So name the files in a `dispatch:local-ok` issue. The test image needs
Docker; the first run builds it (network needed for that build only).

Measured on 2026-11-27 (a one-line docs change, no push): Aider about
25 s, the whole try about 40 s once the image exists.

## Discord posts

The only messages are `#<n> brief ready` and `#<n> local PR #<p> opened`:
no issue text, route reason, code or file contents. Mentions are always off
(`allowed_mentions: {"parse": []}`) and link previews are suppressed.
Without `DISPATCH_DISCORD_WEBHOOK`, briefs are still written and nothing
is posted.

## State

-   `.dispatch/state.json` (git-ignored, written atomically, saved after
    each issue) keeps `briefed`, `announced` and `local` issue numbers,
    `opened` (issue → pull request) and `tries` (failed local tries).
    A recorded issue is skipped before the trust check, the rules and the
    model, so it never causes a model call again.
-   A post is recorded in `announced` only when Discord accepts it. A
    failed post prints a short error and is tried again on the next run,
    without a model call. Nothing retries in a loop.
-   Issues skipped by the trust check are not recorded; they are checked
    again next run.
-   The dry run's `posted` key is read and dropped, so those issues are
    sorted again and get briefs.
-   One run at a time (`.dispatch/run.lock`); a second run stops at once.
-   A broken state file stops the run (`state error`) rather than
    repeating work. Delete it to start over.

### Timer (fish)

Every 15 minutes as a systemd user timer, from a **main-only clone**
(`~/Projects/aether-dispatch`), never the working clone, so only merged,
reviewed code runs with the dispatcher's credentials (like the backup
timer, ADR-025 D8). Before each run the service checks that the clone is
on `main` with no local changes, then runs
`git pull --ff-only origin main`. If either fails, the dispatcher does
not run. Its `.dispatch/` state and reports live in that clone.

Create the clone (once):

```fish
git clone https://github.com/BoxDelta/aether.git ~/Projects/aether-dispatch
cd ~/Projects/aether-dispatch
git branch --show-current            # main
uv sync --frozen                     # its own virtual environment (.venv)
cd ~/Projects/aether
```

Install and enable the timer:

```fish
mkdir -p ~/.config/systemd/user
cp scripts/systemd/aether-dispatch-triage.service scripts/systemd/aether-dispatch-triage.timer ~/.config/systemd/user/
grep WorkingDirectory ~/.config/systemd/user/aether-dispatch-triage.service   # %h/Projects/aether-dispatch
systemctl --user daemon-reload
systemctl --user start aether-dispatch-triage.service      # one run now
systemctl --user enable --now aether-dispatch-triage.timer
```

Check it:

```fish
systemctl --user list-timers aether-dispatch-triage.timer
systemctl --user status aether-dispatch-triage.service --no-pager
journalctl --user -u aether-dispatch-triage.service -n 50 --no-pager
git -C ~/Projects/aether-dispatch status --short --branch   # ## main...origin/main, nothing else
```

Remove:

```fish
systemctl --user disable --now aether-dispatch-triage.timer
rm ~/.config/systemd/user/aether-dispatch-triage.service ~/.config/systemd/user/aether-dispatch-triage.timer
systemctl --user daemon-reload
# Optional: also delete the clone, with its state, briefs and reports.
rm -rf ~/Projects/aether-dispatch
```

## Settings

Read only from the file named by `DISPATCH_ENV_FILE` (default
`~/.config/aether-dispatch/dispatch.env`), never from the shell
environment. The file must be mode 600. Variable names:

-   `DISPATCH_GITHUB_TOKEN`: fine-grained token for this repository only
    (ADR-030 §Credentials).
-   `DISPATCH_LITELLM_KEY`: key for the local LiteLLM proxy.
-   `DISPATCH_LITELLM_URL`: LiteLLM base URL; must be a loopback address,
    for example `http://127.0.0.1:4000`.
-   `DISPATCH_OWNER`: the owner's GitHub login.
-   `DISPATCH_REPO`: the repository name.
-   `DISPATCH_DISCORD_WEBHOOK`: optional; without it nothing is posted. Must
    be `https://discord.com/api/webhooks/...`. The URL is a credential.

The other services keep their own files in the same folder
(`litellm.env`, `llama-swap.env`, `langfuse.env`, `arcane.env`).

## Local model

-   Qwen3.6-35B-A3B Q4_K_M (20 GB GGUF), served by llama.cpp in
    llama-swap.
-   `--n-cpu-moe 35`, 16k context, q8_0 KV cache, flash attention
    (ADR-040 D1; was 33).
-   About 1.9 GiB VRAM stays free when loaded.
-   About 314 tokens/s prompt processing (2.1k-token prompt) and about
    41 tokens/s output (`tests/local-model/README.md` §3).
-   Unloads after 15 idle minutes (`ttl` 900 s, shared with Aether).
-   Thinking is switched off per request
    (`chat_template_kwargs: {"enable_thinking": false}`).
