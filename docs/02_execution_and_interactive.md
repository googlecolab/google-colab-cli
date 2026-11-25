---
log:
2026-11-23: Shared `colab run`'s CPython `SystemExit` handling (`_exit_code_from_outputs`, `_make_script_output_hook`) with `colab exec` and muted IPython's `"To exit: use ..."` warning during `exec` setup. Because a persistent IPython kernel catches `SystemExit` and emits it as `output_type == "error"` to avoid killing the kernel process, treating every kernel error output as exit 1 wrongly failed scripts that call `sys.exit(0)` (e.g. `argparse --help`, `unittest.main()`) and collapsed `sys.exit(N)` codes into 1.

2026-11-23: `colab exec` now exits 1 when executed code produces a kernel `error` output (after recording history and saving `*_output.ipynb` for notebooks, stopping further notebook cells) (#250).

2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' | colab console -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' | colab console -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.

2026-10-24: `--timeout` for `colab exec` (and `colab run`) is now an idle limit: execution fails only after that many seconds without output, so code that keeps printing is never cut off, and `--timeout 0` means no limit. On 0.8.0/0.9.0 the old deadline was never enforced, so code that is silent for longer than `--timeout` now fails where it used to complete. `colab repl` and `colab install`, which pass no timeout, wait until execution finishes, or until the kernel dies or restarts. None of them busy-loop while waiting. `colab auth` and `colab drivemount` are unchanged. See #183.

2026-10-29: Removed the `colab console` subcommand and its `/colab/tty` transport (`console.py`, its tests, and `integration/repro_piped_console/`). Use `colab ssh -s NAME` for an interactive shell and `colab ssh -s NAME -- CMD` for one-shot commands, or pipe `!`-prefixed shell commands to `colab exec` where ssh can't work.
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
- **Error & `SystemExit` Semantics**:
    - Any uncaught exception (`ValueError`, `SyntaxError`, etc.) causes `colab exec` to exit with status `1`
    - Any executed code block that returns an `output_type == "error"` causes colab to exit with status `11
    - Any exit code stops remaining cells in a `.ipynb` notebook while still writing `*_output.ipynb`.
    - **Why `SystemExit` is handled specially**: Unlike CPython—where `sys.exit(...)` terminates the process directly—an IPython/Jupyter kernel must stay alive across executions, so it catches `SystemExit` and reports it as `output_type == "error"` (`ename == "SystemExit"`), accompanied by an interactive `"An exception has occurred, use %tb..."` banner and `"To exit: use 'exit', 'quit', or Ctrl-D."` `UserWarning`. Treating `SystemExit` like an uncaught exception breaks well-formed scripts passed to `colab exec -f script.py` (such as `argparse --help`, `unittest.main()`, or early-exit guards calling `sys.exit(0)`) and loses custom integer exit codes (`sys.exit(N)`). `colab exec` therefore shares `colab run`'s CPython mapping: `sys.exit()` / `sys.exit(None)` / `sys.exit(0)` exit `0` silently, `sys.exit(N)` exits `N` silently, and `sys.exit("msg")` prints `msg` to `stderr` and exits `1`.

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
- **Test Case**: Verify `colab exec` exits with status 1 when the kernel emits an `error` output, and halts multi-cell `.ipynb` execution at the failing cell while still saving `*_output.ipynb`.
- **Test Case**: Verify `colab exec` applies CPython `SystemExit` semantics (`sys.exit(0)`/`None` → `0` silently, `sys.exit(N)` → `N`, `sys.exit("msg")` → prints `msg` to `stderr` and exits `1`) without IPython `%tb` or exit-warning noise.

### 2. TTY and Piping
- **Test Case**: Mock `sys.stdin.isatty()` to verify `colab repl` correctly switches between interactive mode and one-shot piped execution.
- **Test Case**: Verify large piped inputs are handled without buffer overflow or truncation.
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
