# Colab CLI

A command-line interface for Google Colab. Create sessions, run code, manage files, and capture work — all without leaving your terminal.

> Why? The agents are coming.

## Install

```bash
uv tool install git+https://github.com/googlecolab/google-colab-cli
```

Note: If you have a non-standard default package index (Googlers), you may also need to add `--index https://pypi.org/simple`.


## Quick start

```bash
colab new                              # provision a CPU session
echo "print('hello')" | colab exec     # run code
colab stop                             # release the VM
```

When only one session is active you can omit `-s <session>`; the CLI selects it automatically.

## Commands

### Sessions
| Command | Description |
| --- | --- |
| `colab new [-s NAME] [--gpu T4\|L4\|A100\|H100] [--tpu v5e1\|v6e1]` | Provision a new session (CPU by default) |
| `colab sessions` | List all active sessions on the backend |
| `colab status [-s NAME]` | Show one session, or all locally-known sessions |
| `colab stop -s NAME` | Terminate a session |
| `colab url [-s NAME] [--open]` | Print a browser URL that opens the session in Colab |

### Execution
| Command | Description |
| --- | --- |
| `colab exec [-s NAME] [-f FILE] [--output-image PATH]` | Run Python from stdin, a `.py` file, or a `.ipynb` notebook |
| `colab repl [-s NAME] [--output-image PATH]` | Interactive Python REPL (or one-shot if stdin is piped) |
| `colab console [-s NAME]` | Raw TTY shell on the VM (or one-shot if stdin is piped) |

### Files
| Command | Description |
| --- | --- |
| `colab ls [-s NAME] [PATH]` | List remote files |
| `colab upload -s NAME LOCAL REMOTE` | Upload a file |
| `colab download -s NAME REMOTE LOCAL` | Download a file |
| `colab rm -s NAME PATH` | Delete a remote file |
| `colab edit -s NAME PATH` | Edit a remote file in `$EDITOR` |

### Automation & utility
| Command | Description |
| --- | --- |
| `colab auth -s NAME` | Authenticate the VM for GCP services |
| `colab drivemount -s NAME [PATH]` | Mount Google Drive (default `/content/drive`) |
| `colab install -s NAME [-r requirements.txt \| pkg ...]` | Install packages with `uv` (falls back to `pip`) |
| `colab log [-s NAME] [-n N] [-o FILE]` | View or export session history (`.ipynb`/`.md`/`.txt`/`.jsonl`) |
| `colab pay` | Open the Colab signup page |
| `colab version` | Print the installed version |
| `colab update [--install]` | Check for a newer release (and optionally install it) |
| `colab help` | Show usage |

### Global options
- `--auth {oauth2,adc}` — authentication strategy (default `oauth2`)
- `-c, --client-oauth-config PATH` — OAuth client config (default `~/.colab-cli-oauth-config.json`)
- `--config PATH` — session state file (default `~/.config/colab-cli/sessions.json`)
- `--logtostderr` — send all output to stderr

## Examples

```bash
# Train a model on an A100, save the checkpoint locally
colab new -s trainer --gpu A100
colab install -s trainer torch transformers
colab exec -s trainer -f train.py
colab download -s trainer checkpoints/model.bin ./model.bin
colab stop -s trainer

# Mount Drive and analyze a notebook
colab new -s analysis
colab drivemount -s analysis
colab exec -s analysis -f analysis.ipynb       # writes analysis_output.ipynb
colab log -s analysis -o report.ipynb
colab stop -s analysis
```

## Notes

- `repl` and `console` require a TTY when run interactively. Pipe stdin to use them in scripts.
- `exec` reads files locally and ships their contents to the VM — local edits don't require uploading.
- Session metadata is stored at `~/.config/colab-cli/sessions.json`. Settings (auto-update etc.) live at `~/.config/colab-cli/settings.json`.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to file feedback.
