---
lgtm: rtp
date: 2026-03-19
comments: Ready to proceed.
log:

2026-03-18: Implemented `colab exec` subcommand to allow for piped and file-based code execution. Ported `ColabRuntime` to manage Jupyter Kernel communication. Implemented multi-modal output handling (text, png, jpeg, and tracebacks) via Kitty graphics protocol and temp file fallbacks.
2026-03-18: Implemented `colab repl` using `prompt_toolkit`, dropping users into a fully-featured interactive remote python terminal when `sys.stdin.isatty()` is active.
2026-03-19: Implemented `colab console` using a raw TTY websocket connection. This replaces the originally proposed `!magic` wrapper approach with a true SSH-like experience that correctly handles SIGWINCH terminal resizing and ANSI escape sequences.
2026-04-10: Implemented streaming output for `colab exec` and `colab repl`. Updated `ColabRuntime` to support `output_hook` via `execute_interactive` for real-time output delivery.
2026-04-13: Added structured execution output for Jupyter notebooks. `colab exec <file>.ipynb` now evaluates the notebook and saves a new `<file>_output.ipynb` containing all captured multi-modal outputs attached to their respective cells.
2026-04-13: Execution metadata (file, cell, timestamp) is now tracked in `SessionState` for both piped and interactive/REPL code executions.

---
# Design: Execution and Interactive Interaction (`repl`, `exec`, `console`)

## Overview
Execution involves sending Python code (or shell commands) to the Jupyter kernel running on the Colab VM and processing the stream of output messages.

## Approach

### 1. REPL (`colab repl`)
- **Transport**: WebSockets (using `websockets` library if allowed, or a custom `http.client` based long-polling implementation if we're strictly stdlib).
- **Communication**: Jupyter Kernel Messaging Protocol.
    - `execute_request`: Send code string.
    - `execute_reply`: Get status.
    - `iopub.stream`: Capture `stdout` and `stderr`.
- **Interactive Mode**: Standard Python `cmd.Cmd` or `code.InteractiveConsole` for local input/output.
- **Piping Support**: Detect `sys.stdin.isatty()`. If not a TTY, read all input and send as a single execution request.

### 2. Execution (`colab exec`)
- **File Handling**:
    - If file path is local: Read content, send as code.
    - If file path is remote: Execute `!python <path>`.
- **Multi-Modal Output**: Handle `display_data` messages (e.g., `image/png`, `text/html`). For the CLI, we'll save images to temporary files and print their paths, or if the terminal supports it (e.g., iTerm2), inline them.

### 3. Console (`colab console`)
- **Implementation**: Connects directly to the backend terminal endpoint (`/colab/tty`) via WebSockets using `websocket-client`.
- **Interactive**: Bypasses the Jupyter kernel entirely to provide a raw, PTY-backed bash session on the Colab VM.
- **Terminal Management**: Configures `sys.stdin` to raw mode using `termios` and `tty`, passing single characters to the socket and writing raw ANSI escape sequences directly to `sys.stdout.buffer`. Hooks into `SIGWINCH` to communicate local terminal dimensions (`cols`/`rows`) to the remote bash environment so output rendering works perfectly during resizing.

## Implementation Details
- **Kernel Management**: `ColabRuntime` (from `colab-agent`) already handles message signing and message types.
- **Output Streaming**: Continuous polling or asynchronous message handling to provide real-time output.
- **Piping Example**: `cat script.py | colab exec -s my-session`.

## Testing Strategy
TDD is mandatory for all execution features.

### 1. Mock Kernel Client
- **Test Case**: Verify `ColabRuntime` correctly sends an `execute_request` message over the websocket.
- **Test Case**: Verify `iopub.stream` messages are correctly handled and printed to `stdout` in real-time.
- **Test Case**: Verify `display_data` (specifically `image/png`) triggers the correct local handling (saving or display).

### 2. TTY and Piping
- **Test Case**: Mock `sys.stdin.isatty()` to verify `colab repl` correctly switches between interactive mode and one-shot piped execution.
- **Test Case**: Verify large piped inputs are handled without buffer overflow or truncation.
