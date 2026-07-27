# Quick Start: Google Colab CLI on Windows

## Native Windows Support ✅

The Google Colab CLI now **works natively on Windows** — no WSL or Docker required.

## Installation

### Option 1: pip (Recommended for Windows)

```powershell
pip install google-colab-cli
```

### Option 2: uv

```powershell
uv tool install google-colab-cli
```

> **Note:** If `uv tool install` opens GUI windows instead of running in terminal, use `pip install` instead.

### Verify Installation

```powershell
colab version
```

If you see the version number, you're ready! 🎉

## Quick Examples

### Example 1: Hello World

```powershell
colab new
echo "print('Hello from Colab!')" | colab exec
colab stop
```

### Example 2: GPU Training

```powershell
colab new -s training --gpu T4
colab install -s training torch torchvision
colab exec -s training -f train.py
colab download -s training /content/model.pth .\model.pth
colab stop -s training
```

### Example 3: One-Shot GPU Job

```powershell
colab run --gpu T4 experiment.py
```

### Example 4: Run a Notebook Directly

```powershell
colab run --gpu T4 --keep .\Viral_Shorts_Generator.ipynb
```

While developing from this repository, use `uv run colab` so PowerShell runs the local patched CLI:

```powershell
cd "C:\Users\Pc\OneDrive\Desktop\Colab Cli\google-colab-cli"
uv run colab run --gpu T4 --keep "..\long-video-to-shorts-maker\Viral_Shorts_Generator.ipynb"
```

Notebook code cells run in order, output streams back to the terminal, and an output notebook is saved beside the input as `<name>_output.ipynb`.

### Example 5: Interactive REPL

```powershell
colab new --gpu L4
colab repl
```

```python
>>> import torch
>>> torch.cuda.is_available()
True
>>> torch.cuda.get_device_name(0)
'NVIDIA L4'
```

### Example 6: Interactive Console

```powershell
colab new
colab console
```

Full TTY shell with tmux — works natively in Windows Terminal, PowerShell, or CMD.

## Available Hardware

### Free GPUs
- **T4** - Development and small models

### Premium GPUs (Colab subscription)
- **L4** - Cost-effective inference
- **G4** - Balanced performance
- **A100** - High-performance training
- **H100** - State-of-the-art

### TPUs (Colab subscription)
- **v5e1**, **v6e1**

```powershell
colab new --gpu A100
colab new --tpu v5e1
```

## File Operations

```powershell
colab ls /content
colab upload .\data.csv /content/data.csv
colab download /content/results.csv .\results.csv
colab edit /content/script.py
colab rm /content/old_file.txt
```

## Session Management

```powershell
colab sessions                    # List all active sessions
colab status -s training          # Check session status
colab restart-kernel -s training  # Restart kernel
colab url --open                  # Open in browser
colab stop -s training            # Terminate session
```

## Google Drive & Authentication

```powershell
colab drivemount                  # Mount Google Drive
colab auth                        # Authenticate for GCP services
```

## Export Logs

```powershell
colab log -o history.ipynb   # Jupyter notebook
colab log -o history.md      # Markdown
colab log -o history.jsonl   # JSON Lines
```

## Tips

1. **Single session**: When only one session is active, omit `-s name`:
   ```powershell
   colab exec -f script.py
   ```

2. **Pipe input**: Works in PowerShell and CMD:
   ```powershell
   type script.py | colab exec
   echo "import sys; print(sys.version)" | colab exec
   ```

3. **Automatic keep-alive**: Sessions stay active in the background automatically. Long `run` and `exec` commands also pulse keep-alive while the foreground command is still running.

4. **Update CLI**:
   ```powershell
   colab update --install
   ```

## Troubleshooting

### Command not found

Ensure Python Scripts directory is in PATH:
```powershell
# Add to PATH (PowerShell as Admin)
$env:Path += ";$env:LOCALAPPDATA\Programs\Python\Python312\Scripts"
```

Or use module invocation:
```powershell
python -m colab_cli.cli version
```

### GUI window opens instead of terminal

This happens with `uv tool install`. Solution:
1. Uninstall: `uv tool uninstall google-colab-cli`
2. Reinstall: `pip install google-colab-cli`

## Next Steps

- [Main README](README.md) - Full documentation
- [Demo Walkthroughs](docs/demos.md) - Real-world examples
- [Session Management](docs/01_session_management.md) - Architecture deep-dive

---

Native Windows support tested on Windows 11 with Python 3.13, PowerShell, and CMD. 🚀
