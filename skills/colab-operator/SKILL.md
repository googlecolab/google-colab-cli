---
name: colab-operator
description: Operate Google Colab environments via the `colab` CLI. Use when asked to create or manage GPU/TPU sessions, run Python/shell on a remote Colab VM, sync files, automate environment setup (packages, auth, Drive), or export session history.
---

# Skill: Colab Session Operator

Operate Google Colab environments via the `colab` CLI: provision GPU/TPU sessions, run Python/shell on the VM, sync files, and capture work as notebooks.

## Installation

If the user does not already have the `colab` tool installed, it can be acquired
by running `uv tool install google-colab-cli` or `pip install google-colab-cli`.

## When to activate
- Creating or managing TPU/GPU sessions.
- Running Python or shell on a remote Colab VM.
- Syncing files between local and remote.
- Automating environment setup (packages, auth, Drive).
- Exporting session history as a Jupyter notebook.

## Mental model (read this first)
- **A session == a live Jupyter kernel on a rented VM.** `colab new` allocates a billable VM; `colab stop` releases it. Sessions stay alive as long as the kernel is active, with the Colab backend automatically maintaining liveness.
- **Kernel state PERSISTS across `colab exec` / `colab repl` calls in the same session.** Each invocation reattaches to the *same* kernel (the kernel ID is cached in local state) and only closes the websocket on exit — it does **not** shut the kernel down. So imports, variables, and defined functions survive between separate `colab exec` commands. Build up state incrementally; don't re-import everything each call. (`colab stop` and `colab restart-kernel` are what actually reset it.)
- **Default working directory is `/content`.** Every `exec`/`repl`/`run` `cd`s there first; prefer absolute paths (`/content/...`) for file work. For `colab ls/rm/upload/download`, absolute `/content/...` paths work and the default `ls` path is `content` (VM root).
- **`colab` is fire-and-forget.** Each command authenticates, does one thing, and exits.

## Authentication (the #1 thing that blocks agents)
- The global flag is `--auth={adc,oauth2}` and the **default is `adc`** (Application Default Credentials). It must come *before* the subcommand: `colab --auth=adc new -s x`.
- **ADC setup** (most reliable for headless/agent use). The Colab backends need a specific scope set, so re-mint ADC with all four scopes:
  ```bash
  gcloud auth application-default login \
    --scopes=openid,\
  https://www.googleapis.com/auth/cloud-platform,\
  https://www.googleapis.com/auth/userinfo.email,\
  https://www.googleapis.com/auth/colaboratory
  ```
  Why all four: `userinfo.email` (session backend `colab.research.google.com`, else 401), `colaboratory` (Colab features/APIs), `openid`+`cloud-platform` (mandated by gcloud itself; it rejects scope lists missing `cloud-platform`).
- **oauth2 setup**: `colab --auth=oauth2 <anything>` triggers a browser consent flow on first use (token cached in the OS keyring via `keyring`). Requires a client config at `~/.colab-cli-oauth-config.json` (or `-c PATH`), falling back to the bundled config. The browser step means it usually needs a human; prefer ADC for agents.
- **Verify auth in one shot**: `colab sessions` (read-only, lists server assignments) or `colab whoami` (hidden debug command: prints the active email, scopes, audience, and expiry). When any call 401s or 403s, the cause is almost always missing credentials or scopes — `colab whoami` shows it instantly.
- **Do NOT confuse `colab auth` with CLI authentication.** `colab auth` injects *VM-side* GCP credentials into the running kernel (so notebook code can call BigQuery/GCS); it is orthogonal to how the CLI itself authenticates. Never suggest "run `colab auth`" to fix a CLI 401/403 — that's a scope/identity problem fixed via the `gcloud` command above.

## Workflow

### Provision
- `colab new -s <name>` (CPU). Add `--gpu A100` or `--tpu v6e1` for accelerators. **Always pass `-s <name>`** — an omitted name is auto-generated as a random 6-hex string, which makes later commands ambiguous.
- Supported `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. Supported `--tpu`: `v5e1`, `v6e1`.
- **Gotcha**: an unrecognized `--gpu` value silently falls back to **A100** (which then usually fails the next step). A `400` on `colab new` with an accelerator means no quota/entitlement for it on this account — fall back to `--gpu T4` or omit the flag for CPU.
- Accelerator availability is tier-gated; most accounts can only get CPU. Don't assume a GPU/TPU will allocate.

### Execute
- **Preferred**: `colab exec -s <name> -f <script.py>` runs a local script on the remote VM (read locally, sent to the kernel — no manual upload needed).
- **Piped code**: `echo "print(1)" | colab exec -s <name>` or `cat script.py | colab exec -s <name>`.
- **Notebooks**: `colab exec -s <name> -f nb.ipynb` runs each code cell and writes results to `<basename>_output.ipynb` next to the input. A `# @title Foo` first line labels the cell in progress output.
- **Plots/images**: PNG/JPEG outputs are intercepted. Use `--output-image <path>` on `exec`/`repl` to save to a known location (otherwise a temp path is printed). Inline terminal-image escapes are auto-suppressed when stdout isn't a TTY, so piped/captured output stays clean.
- **Shell**: run a one-shot shell command with `colab ssh -s <name> -- <cmd>`: it runs in `/content` and exits with the command's status. Output is byte-exact when stdout is piped or redirected; ssh allocates a PTY only when stdin and stdout are both terminals, so a shell tool that runs commands in a terminal should add `</dev/null` and redirect the output to a file (a pipe keeps `colab ssh`'s exit status only under `set -o pipefail`). One argument is a shell command line (quote it for pipes or globs: `colab ssh -s <name> -- 'nvidia-smi | head -5'`); several arguments are an exact argv, so pass untrusted values as separate arguments after `--` where the program accepts it (e.g. `colab ssh -s <name> -- ls -- "$f"`), never inside a shell line or a piped script. Pipe a multi-line script to `colab ssh -s <name> -- bash`. Use `colab exec` for Python code. When ssh can't work (no local `ssh` client, no `~/.ssh/id_ed25519` or `id_ecdsa` key and no `-i KEY`, a tool not on the minimal ssh environment's `PATH` such as `gcloud` or `node`, or the runtime's limited concurrent SSH connections in use, which the server answers with 429, e.g. while a person or an IDE is connected), fall back to `exec` with IPython's `!` prefix: `echo '!df -h /content' | colab exec -s <name>` (single quotes stop your local shell expanding `!`). Each `!` line runs in its own shell with no input (a command that prompts hangs), and IPython evaluates `{expr}` as Python even inside quotes and expands `$name` outside single quotes. Start a multi-line script with a `%%bash` line (not with `--env`, which prepends Python lines), use non-interactive flags such as `apt-get -y`, and never put untrusted text in a `%%bash` script or a `!` line (even via `{var}` or `$var`): pass it with `--env NAME=VALUE` and run `subprocess.run(["cmd", "--", os.environ["NAME"]])` from Python. For an interactive shell on the VM, a human runs `colab ssh -s <name>`.
- **Never run `colab repl`, `colab ssh` without `-- CMD`, `colab auth`, or `colab drivemount` interactively from an agent** — `repl`, `auth` and `drivemount` expect a TTY and will hang or fail, and bare `colab ssh` with no saved session creates a billable runtime; an interactive shell is for a human. `repl` accepts piped stdin and exits on EOF; `auth`/`drivemount` genuinely require a human at the terminal.

### Ephemeral one-shot jobs (`colab run`)
- `colab run [--gpu T4] [--tpu v6e1] [--keep] [-s NAME] script.py [args...]` = `new` + `exec` + `stop` in one command. It provisions a fresh VM, runs the script with `sys.argv` and `__name__ == "__main__"` set like native `python script.py args`, then tears the VM down (unless `--keep`).
- **Exit codes propagate**: an uncaught exception or `sys.exit(N)` in the script makes `colab run` exit non-zero (CPython semantics: `sys.exit()`/`sys.exit(0)` → 0, `sys.exit(N)` → N, `sys.exit("msg")` → 1).
- **Stream separation**: `colab run` writes its own `[colab] ...` chatter to **stderr** and the script's output to **stdout** — so `colab run job.py > out.txt` captures only the script's stdout. (`colab exec` streams the script's stdout/stderr live to your stdout/stderr.)
- Works as a shebang: `#!/usr/bin/env -S colab run --gpu T4` makes a `chmod +x`'d `.py` a self-contained "rent a GPU, run, clean up" script. After editing CLI behavior, reinstall before testing shebangs — they resolve `colab` via `$PATH`, not the editable install.
- A nonexistent script path exits non-zero **before** allocating a VM (no wasted compute).

### Automate
- `colab auth -s <name>` — VM-side GCP creds, needed before in-VM GCS/BigQuery calls (interactive; not agent-runnable).
- `colab drivemount -s <name> [PATH]` — mounts Drive at `/content/drive` by default (interactive; not agent-runnable).
- `colab install -s <name> pkg1 pkg2` — installs via `uv pip install --system`, falling back to `pip`. Also `colab install -s <name> -r requirements.txt`.

### Inspect & report
- `colab help` (or `colab help <cmd>`) lists/explains commands; the listing is alphabetical.
- `colab sessions` lists server-side assignments and auto-prunes stale local entries. Orphans with no local record show as `[?]`.
- `colab status [-s <name>]` shows hardware, IDLE/BUSY, and last execution.
- `colab log -s <name> [-n 20] [-t TYPE]` shows recent structured events; invaluable when a task fails.
- `colab log -s <name> -o summary.ipynb` exports the session as a notebook (also `.md`, `.txt`, `.jsonl` by suffix).
- `colab url -s <name>` prints a browser URL that attaches the Colab web UI to your existing CLI session instead of allocating a new VM (add `--open` to launch it).
- `colab skill` / `colab readme` print this skill and the README (handy for self-discovery).

## Safety
- **Always `colab stop -s <name>` when done** — idle VMs burn compute units. `colab run` (without `--keep`) self-cleans even if the script errors.
- Local state lives in `~/.config/colab-cli/sessions.json` (settings in `settings.json`, history in `history/*.jsonl`). Don't edit by hand.
- **Isolate parallel/agent runs** with the global `--config <path>` flag to point session state at a scratch file (e.g. `colab --config /tmp/agent.json new -s job`).

## Recovery
- "Session not found" / 404 / 401 on exec: the backend pruned the VM. `colab exec`/`repl` detect this and clean up local state automatically — run `colab sessions` and re-create with `colab new`.
- Execution timeout or wedged kernel: `colab restart-kernel -s <name>` (keeps the VM, resets the kernel), or `colab stop` then `colab new`.
