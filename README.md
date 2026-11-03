# Colab CLI

A command-line interface for Google Colab. Provision high-performance CPU, GPU, and TPU runtimes, execute local code, manage remote files, and orchestrate automated cloud pipelines — directly from your terminal.

Designed to support seamless developer productivity, headless automation, and AI agent integrations.

[Demo](https://github.com/user-attachments/assets/656226a9-af13-4fdb-8eda-d7de747336a2)

> [!NOTE]
> **Platform support:** the Colab CLI currently supports **Linux and macOS** only. Windows is not supported at this time.

> [!TIP]
> Looking for in-notebook, interactive agent-assisted coding instead of a terminal workflow? See the [Colab MCP Server](https://github.com/googlecolab/colab-mcp).

---

## Key Features

* **Instant VM Provisioning:** Spin up CPU, GPU (T4, L4, G4, H100, A100), or TPU (v5e1, v6e1) runtimes in seconds.
* **Robust Code Execution:** Run local Python scripts, Jupyter Notebooks (`.ipynb`), or piped `stdin` code; launch interactive REPLs or SSH shells (`colab ssh`).
* **Ephemeral Job Runner (`colab run`):** Provision a fresh VM, execute a local script with forwarded arguments, retrieve output files, and automatically tear down the runtime in a single command.
* **Activity-Based Liveness:** Sessions stay alive as long as the kernel is active, with the Colab backend automatically maintaining liveness.
* **Seamless Workspace Automation:** Mount Google Drive, authenticate Google Cloud Platform (GCP) credentials, and install dependencies with high-performance `uv` package management.
* **State & Log Archival:** Inspect local session states or export interactive history logs to standard Jupyter Notebooks, Markdown, or structured JSONL.

---

## Installation

Install the package using `uv` (recommended) or standard `pip`:

```bash
# Using uv (recommended)
uv tool install google-colab-cli

# Using pip
pip install google-colab-cli
```

---

## Quick Start

Run a CPU-based VM runtime, execute some code, and clean up:

```bash
# 1. Provision a new session
colab new

# 2. Execute code from stdin
echo 'print("Hello from Google Colab!")' | colab exec

# 3. Stop and release the VM resource
colab stop
```

> [!NOTE]
> When only one session is active, you can omit the `-s, --session` option;
> the CLI automatically knows it.


---

## Command Index

Run `colab <command> --help` to view specific options, defaults, and detailed help.

### Session Management
| Command | Description |
| --- | --- |
| `colab new [-s NAME] [--gpu GPU] [--tpu TPU] [--high-mem]` | Allocate a new CPU, GPU, or TPU VM runtime (optionally high-RAM) |
| `colab sessions` | List all active sessions currently active on the backend |
| `colab status [-s NAME]` | Display hardware, machine shape, status, and local metadata for active sessions |
| `colab restart-kernel [-s NAME]` | Restart the active session's Jupyter kernel |
| `colab stop [-s NAME]` | Terminate a session VM and release its resources |
| `colab url [-s NAME] [--open]` | Print or open a browser URL connecting to the active session |

### Execution
| Command | Description |
| --- | --- |
| `colab run [--gpu GPU] [--tpu TPU] [--high-mem] [--keep] SCRIPT [ARGS...]` | Run a local script on a fresh VM, forwarding arguments, then release it |
| `colab exec [-s NAME] [-f FILE] [--output-image PATH]` | Execute Python code from stdin, a local `.py` file, or a `.ipynb` notebook |
| `colab repl [-s NAME] [--output-image PATH]` | Start an interactive Python REPL on the VM (exits cleanly on piped EOF) |
| `colab ssh [-s NAME] [--proxy-mode] [-i KEY] [--gpu GPU] [--tpu TPU] [--high-mem] [-- CMD ...]` | Open an SSH shell to the runtime over WebSocket, run a one-shot remote command (`-- CMD`), or act as an OpenSSH `ProxyCommand` bridge for IDE remote-dev |

### File Operations
| Command | Description |
| --- | --- |
| `colab ls [-s NAME] [PATH]` | List remote files on the VM |
| `colab upload [-s NAME] LOCAL REMOTE` | Upload a local file to the VM filesystem |
| `colab download [-s NAME] REMOTE LOCAL` | Download a remote file from the VM filesystem |
| `colab rm [-s NAME] PATH` | Delete a remote file on the VM filesystem |
| `colab edit [-s NAME] PATH` | Edit a remote file in-place using your local `$EDITOR` |

### Automation & Utilities
| Command | Description |
| --- | --- |
| `colab auth [-s NAME]` | Authenticate the VM for GCP services (BigQuery, GCS, etc.) |
| `colab drivemount [-s NAME] [PATH]` | Mount Google Drive on the VM (default: `/content/drive`) |
| `colab install [-s NAME] [-r FILE \| PKG...]` | Install packages on the VM using `uv` (falls back to `pip`) |
| `colab log [-s NAME] [-n N] [-o FILE]` | View or export session history (`.ipynb`, `.md`, `.txt`, `.jsonl`) |
| `colab usage` | Show account compute-unit usage rate and balance |
| `colab pay` | Open the Colab subscription page to manage compute units |
| `colab version` | Print the installed version of the CLI |
| `colab update [--install]` | Check for a newer release (and optionally upgrade the CLI in place) |

### Global Options
* `--auth {oauth2,adc}` — Authentication strategy for the Colab API (default: `adc`).
* `-c, --client-oauth-config PATH` — Path to public OAuth client credentials configuration (default: `~/.colab-cli-oauth-config.json`).
* `--config PATH` — Path to local session metadata storage (default: `~/.config/colab-cli/sessions.json`).
* `--logtostderr` — Direct debug logging output to stderr.

---

## Practical Examples

### Accelerator Training with Checkpoint Retrieval

Provision an A100 GPU, install requirements, run a local training script, retrieve the resulting model weights, and terminate the VM:

```bash
colab new -s trainer --gpu A100
colab install -s trainer torch transformers
colab exec -s trainer -f train.py
colab download -s trainer checkpoints/model.bin ./model.bin
colab stop -s trainer
```

### Workspace Notebook Execution with Drive Integration

Mount Google Drive, run a local notebook against the VM kernel (outputs are written back into `report_output.ipynb`), export a Markdown log of the execution, and clean up:

```bash
colab new -s analysis
colab drivemount -s analysis
colab exec -s analysis -f report.ipynb
colab log -s analysis -o execution_log.md
colab stop -s analysis
```

---

## Usage Notes

* **Machine shape:** Use `--high-mem` with `colab new`, `colab run`, or `colab ssh` (when auto-creating a runtime) to request a high-RAM machine shape. Requires Colab Pro or Pro+ entitlement for supported accelerators (CPU, T4, A100, etc.). L4 and TPU runtimes ignore this flag because they only offer one shape. Machine shape is shown in `colab sessions` and `colab status`.
* **TTY Requirements:** `colab repl` and interactive `colab ssh` expect a local TTY. When running inside automated scripts or pipelines, pipe stdin to `colab repl` (e.g., `echo "print(1)" | colab repl`) to trigger its non-interactive mode, or run one-shot shell commands with `colab ssh -s NAME -- CMD` (e.g., `colab ssh -s NAME -- ls /content`); pass untrusted values as separate arguments (after `--` where the program accepts it), since a single argument is a remote shell line. `colab ssh` needs a local `ssh` client and key, so where it can't work (no client or key, a tool not on the minimal ssh environment's `PATH`, or the runtime's concurrent SSH connections in use), pipe the commands to `colab exec` with IPython's `!` prefix (e.g., `echo '!ls /content' | colab exec`); each `!` line is its own shell and IPython expands `{expr}` and `$name` in it, so start a multi-line script with `%%bash` and keep untrusted text out of `!` lines.
* **Transparent Code Execution:** When calling `colab exec -f file.py`, the CLI reads the file locally and transmits its content to the remote kernel. You do not need to manually upload files before execution.
* **Storage & State Paths:** Session tokens and metadata are stored at `~/.config/colab-cli/sessions.json`. Global CLI settings are located at `~/.config/colab-cli/settings.json`. These can be customized or isolated via the global `--config` flag.

### Ephemeral Accelerator Jobs

Use `colab run` to run a local script on dedicated hardware without manual session lifecycle management. The CLI handles provisioning, script execution, and immediate VM teardown automatically:

```bash
# Run train.py on a T4 GPU and release the VM on completion
colab run --gpu T4 train.py
```

### Shebang Execution Support

To execute a local file directly on a remote accelerator, place the `colab run` interpreter in the shebang line:

```python
#!/usr/bin/env -S colab run --gpu L4 --keep
import torch

print("L4 GPU Available:", torch.cuda.is_available())
print("Device Name:", torch.cuda.get_device_name(0))
```

Make the script executable (`chmod +x script.py`) and run it: `./script.py`. The `--keep` option tells the CLI to preserve the session VM on completion so you can re-execute or inspect logs.

---

## Deep Dive Documentation

For comprehensive architectural overviews and deep-dives into specific CLI sub-systems, refer to the detailed documentation:

* [Session Management Architecture](docs/01_session_management.md)
* [Interactive & Non-Interactive Execution Design](docs/02_execution_and_interactive.md)
* [File Management & Jupyter Contents API](docs/03_file_management.md)
* [Authentication Providers & VM Automation](docs/04_automation_and_utility.md)
* [Ephemeral Job Runner Design](docs/05_run_command.md)

To view interactive walkthroughs of eleven real-world automated scenarios, check out the [Demo Walkthroughs](docs/demos.md).

---

## Contributing

Feedback and contributions are welcome! Please read [`CONTRIBUTING.md`](./CONTRIBUTING.md) for details.

---

## Legal & Privacy

See Colab's [Terms of Service](https://research.google.com/colaboratory/tos_v6.html) and the [Google Privacy Policy](https://policies.google.com/privacy), which apply to usage of this CLI.
