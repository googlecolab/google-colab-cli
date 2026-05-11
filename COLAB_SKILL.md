# Skill: Colab Session Operator

Operate Google Colab environments via the `colab` CLI: provision GPU/TPU sessions, run Python/shell on the VM, sync files, and capture work as notebooks.

## When to activate
- Creating or managing TPU/GPU sessions.
- Running Python or shell on a remote Colab VM.
- Syncing files between local and remote.
- Automating environment setup (packages, auth, Drive).
- Exporting session history as a Jupyter notebook.

## Workflow

### Provision
- `colab new -s <name>` (CPU). Add `--gpu A100` or `--tpu v6e1` for accelerators.
- If only one session is active, `-s` may be omitted on most command.
- After `colab new`, run `colab status` to confirm the VM is responsive.

### Execute
- **Preferred**: `colab exec -s <name> -f <script.py>` — runs a local script on the remote VM. The kernel `cd`s to `/content` first.
- **Plots**: PNG/JPEG outputs are intercepted automatically. Use `--output-image <path>` on `exec`/`repl` to save to a known location; otherwise a temp file path is printed.
- **Shell**: `echo "cmd" | colab console -s <name>` works for batch shell. `exec` is faster when you don't need a real shell.
- **Never run `colab repl` or `colab console` interactively from an agent** — they expect a TTY and will hang. Always pipe stdin.

### Automate
- `colab auth -s <name>` — needed before GCP services (GCS, BigQuery).
- `colab drivemount -s <name>` — exposes `/content/drive/MyDrive`.
- `colab install -s <name> pkg1 pkg2` — uses `uv` for speed.

### Inspect & report
- `colab help` lists every command.
- `colab log -s <name> -n 20` shows recent actions; useful when a task fails.
- `colab log -s <name> -o summary.ipynb` produces a notebook artifact of the session.

## Safety
- **Always `colab stop -s <name>` when done** — idle VMs burn compute units.
- Local session state lives at `~/.config/colab-cli/sessions.json`. Don't edit by hand.
- If `colab auth` fails, ask the user to verify their gcloud / OAuth credentials.

## Recovery
- "Session not found": the backend may have pruned it. Run `colab sessions` and re-create if needed.
- Execution timeout: kernel may be deadlocked. `colab stop` then `colab new`.
