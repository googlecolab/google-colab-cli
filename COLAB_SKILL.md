# Skill: Colab Session Operator

This skill transforms an agent into an expert at managing and executing tasks within Google Colab environments using the `colab` CLI.

## **When to Activate**
Activate this skill when the user requests:
- Creating or managing high-performance compute (TPU/GPU) sessions.
- Executing Python scripts or Shell commands on a remote Colab VM.
- Syncing files between local and remote environments.
- Automating environment setup (packages, auth, Drive).
- Recording or exporting work history as Jupyter Notebooks.

## **Core Workflows**

### **1. Environment Provisioning**
- **Creation**: Use `colab new -s <name>`. Like all other commands, this requires the `-s` flag for a custom name.
- **Hardware Selection**: 
  - Use `-tpu v6e1` for JAX/TPU training.
  - Use `-gpu A100` for high-memory LLM tasks.
- **Session Defaults**: If you only have one active session, you can omit the `-s <name>` flag for all commands (e.g., just run `colab status` or `colab exec`). The CLI will auto-default to the unique active session.
- **Verification**: After `colab new`, run `colab status` to ensure the VM is responsive.

### **2. Execution Strategy**
- **Script Execution (Recommended)**: Use `colab exec -s <name> -f <local_script.py>` to run a local script on the remote VM. The kernel is automatically switched to `/content` before execution.
- **Visuals & Plots**: The CLI automatically intercepts image data (`image/png`, `image/jpeg`). 
  - *Redirection*: Use `--output-image <path>` with `exec` or `repl` to save the plot directly to your desired location.
  - *Fallback*: If no path is provided, it saves to a **local temporary file** and prints the path.
- **Kernel Warmup**: On fresh VMs, the first execution may take longer. The CLI includes built-in retry logic with exponential backoff to handle these initial startup timeouts.
- **Batch Shell**: Use `echo "command" | colab console -s <name>` for raw shell tasks. Be aware that this launches a `tmux` session on the VM, which may add slight overhead. Use `colab exec` for faster, cleaner output when possible.
- **Interactive Hazards**: **NEVER** run `colab repl` or `colab console` without piped input. These commands expect a TTY and will cause the agent to hang indefinitely.

### **3. Automation & State**
- **Authentication**: Run `colab auth -s <name>` immediately after creation if GCP services (GCS, BigQuery) are needed.
- **Storage**: Mount Drive via `colab drivemount -s <name>` to access `/content/drive/MyDrive`.
- **Installations**: Use `colab install -s <name> package1 package2` for rapid setup (uses `uv` for speed).

### **4. Discovery & Logging**
- **Help**: Run `colab help` or `colab --help` to see all commands. Running `colab` without arguments also shows the help menu.
- **Session History**: All agent executions are automatically recorded.
- **Reporting**: Before stopping a session, run `colab log -s <name> -o summary.ipynb` to provide the user with a permanent artifact of the agent's work.
- **Timeline**: Use `colab log -s <name> -n 20` to verify the last few actions if a task fails.

## **Safety & Constraints**
- **Cost Control**: Always run `colab stop -s <name>` when tasks are finished to avoid wasting compute units.
- **State Sensitivity**: Local session metadata is stored in `~/.config/colab-cli/sessions.json`. Do not delete this file manually.
- **Auth Fallbacks**: If `colab auth` fails, prompt the user to check `gcert` or their OAuth credentials.

## **Error Recovery**
- **"Session not found"**: The session might have been pruned by the backend. List active sessions and re-initialize if necessary.
- **"Execution Timeout"**: The kernel may be deadlocked. Try `colab stop` and `colab new` to restart the environment.
