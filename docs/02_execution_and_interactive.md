---
log:
2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' | colab console -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' | colab console -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.

2026-10-24: `--timeout` for `colab exec` (and `colab run`) is now an idle limit: execution fails only after that many seconds without output, so code that keeps printing is never cut off, and `--timeout 0` means no limit. On 0.8.0/0.9.0 the old deadline was never enforced, so code that is silent for longer than `--timeout` now fails where it used to complete. `colab repl` and `colab install`, which pass no timeout, wait until execution finishes, or until the kernel dies or restarts. None of them busy-loop while waiting. `colab auth` and `colab drivemount` are unchanged. See #183.

2026-10-29: Removed the `colab console` subcommand and its `/colab/tty` transport (`console.py`, its tests, and `integration/repro_piped_console/`). Use `colab ssh -s NAME` for an interactive shell and `colab ssh -s NAME -- CMD` for one-shot commands, or pipe `!`-prefixed shell commands to `colab exec` where ssh can't work.

2026-11-11: Added `colab exec --json`. Each executed cell becomes one JSON object on stdout (NDJSON) instead of streamed text, `[colab]` progress lines move to stderr, and the exit status is 1 when any cell failed. Without the flag, `colab exec` is unchanged (it still exits 0 after a kernel error).
---

# Design: Execution and Interactive Interaction (`repl`, `exec`)

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
- **Timeout Configuration**: Exposes a `--timeout` flag (default 30s): the longest silence allowed before execution fails. Raise it so long-running silent tasks (like model compilation or data downloading) are not prematurely killed; `0` means no limit.
- **Machine-Readable Output (`--json`)**: For scripts and AI agents that must decide what to do next from the result. Plain `colab exec` exits 0 after a kernel error and prints an ANSI-coloured traceback, so a caller can only guess at failure by scraping text. With `--json`:
    - stdout carries one JSON object per executed cell, one per line (NDJSON), written when the cell finishes. Nothing else is written to stdout; `[colab]` progress lines go to stderr.
    - Fields: `cell_index`, `cell_id` (notebook cell id, else `null`), `status` (`"ok"` or `"error"`), `stdout`, `stderr` (the kernel's streams, concatenated), `data` (one object per `execute_result` / `display_data` output: its `output_type` plus its non-image MIME entries such as `text/plain`), `images` (paths of the PNG/JPEG outputs saved to disk, honouring `--output-image`; never inline base64), and `error` (`null`, or `ename`, `evalue` and `traceback` as plain text with ANSI escapes removed).
    - The exit status is 1 when any cell has `status: "error"`. Notebook cells after a failing cell still run, as without the flag.
    - A client-side failure (idle `--timeout`, lost connection) is reported as a final record whose `error.ename` is the exception type, before the command fails as it does without the flag.

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
- **Test Case**: `tests/test_exec_json.py` covers the `--json` record for success, kernel error (ANSI stripped, exit 1), images (saved, path reported, no base64 on stdout), per-cell notebook records, client-side failures, a lost session, and that the default mode still exits 0 on a kernel error.
- **Test Case**: Mock `sys.stdin.isatty()` to verify `colab repl` correctly switches between interactive mode and one-shot piped execution.
- **Test Case**: Verify large piped inputs are handled without buffer overflow or truncation.
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
