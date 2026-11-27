# Dispatcher (prototype)

Development tooling from ADR-030, not part of Aether's runtime. Built in
steps 4a to 4d; only **4a (dry-run triage)** exists so far. It sorts
`dispatch:auto` issues into `local` or `claude` and reports the result.
It never writes to GitHub.

## Commands (fish, from the repo root)

-   `uv run python -m dispatch triage`: sort every open `dispatch:auto`
    issue.
-   `uv run python -m dispatch triage --issue 123`: sort one issue.
-   `uv run python -m dispatch triage --notify`: sort only issues not
    posted before and post each result to Discord (see below).
-   `uv run python -m dispatch eval tests/dispatch/fixtures/triage_set.yaml`:
    sort a fixed set of past issues and compare with the expected route.
    Exit code 1 if any issue misses.

`triage` prints a table (issue, route, step, reason) and appends it to
`.dispatch/reports/<date>.md` (git-ignored).

## How an issue is sorted

1.  **Trust** (step `trust`): the issue must be opened by the owner, carry
    `dispatch:auto`, and the latest `labeled` event for that label must be
    by the owner. Otherwise it is skipped. Pull requests are skipped.
    `eval` skips only the label part, never the author check.
2.  **Rules** (step `rule`): the always-Claude list in `rules.py`, checked
    against labels and against paths or keywords in the title and body.
    A match means `claude` with no model call.
3.  **GPU** (step `gpu`): waits only when something other than our model
    uses the GPU. Skipped for the rest of a run once a model call in that
    run has answered. Before that, the GPU counts as available if
    `nvidia-smi --query-compute-apps` lists only `llama-server` processes
    (`gpu.py`). Otherwise `router/hardware.py` must report the VRAM
    reserve free; it waits up to 5 minutes, polling every 15 seconds,
    then gives `claude`.
4.  **Model** (step `model`): one LiteLLM call, model `local`, temperature
    0, 200 tokens, thinking off, Langfuse trace `dispatch-triage` tagged
    `dry-run` and `issue-<n>`. The answer must be exactly
    `{"route": "local"|"claude", "reason": "..."}`; anything else, a
    timeout or an HTTP error means `claude`.

## Discord posts (until the 4a review on 2026-12-03)

`triage --notify` posts one message per sorted issue to the webhook:
`#<n> → <route> (<step>): <reason>` and the issue link. The issue title,
body and code are never put in the message, but a model-written reason
may quote up to 200 characters of them. The reason is cleaned (one line, at most 200
characters), `@` is removed and Discord markdown is escaped; mentions are
always off (`allowed_mentions: {"parse": []}`) and link previews are
suppressed.

-   Posted issue numbers are kept in `.dispatch/state.json` (git-ignored,
    written atomically). The state is checked before the trust check, the
    rules and the model, so a posted issue never causes a model call.
-   An issue is recorded only when Discord accepts the post. If Discord
    fails, the run prints a short error and carries on; the issue is tried
    again on the next run. Nothing retries in a loop.
-   Issues skipped by the trust check are never posted.
-   One `--notify` run at a time (`.dispatch/notify.lock`); a second run
    stops at once.
-   A broken state file stops the run (`state error`) rather than posting
    everything again. Delete it to start over.

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
# Optional: also delete the clone, with its posted-issue state and reports.
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
-   `DISPATCH_DISCORD_WEBHOOK`: optional, needed only for `--notify`. Must
    be `https://discord.com/api/webhooks/...`. The URL is a credential.

The other services keep their own files in the same folder
(`litellm.env`, `llama-swap.env`, `langfuse.env`, `arcane.env`).

## Local model

-   Qwen3.6-35B-A3B Q4_K_M (20 GB GGUF), served by llama.cpp in
    llama-swap.
-   `--n-cpu-moe 33`, 16k context, q8_0 KV cache, flash attention.
-   7,415 MiB VRAM when loaded.
-   About 350 tokens/s prompt processing (2.7k-token prompt) and 42 to 44
    tokens/s output.
-   Unloads after 5 idle minutes.
-   Thinking is switched off per request
    (`chat_template_kwargs: {"enable_thinking": false}`).
