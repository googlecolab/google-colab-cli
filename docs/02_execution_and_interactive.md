---
log:
2026-11-09: Recover the original execution's output and result with a request-specific journal, including completion during an outage. Refresh rejected runtime proxy credentials and routes for the same assignment, and continue through HTTP if WebSocket reconnection stops. Persist identity before submission; `colab resume` recovers saved work after CLI exit without starting a kernel or resubmitting code. Bound unavailable-record and missing-completion waits; preserve uncertain work and its runtime.

2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' | colab console -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' | colab console -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.

2026-10-24: `--timeout` for `colab exec` (and `colab run`) is now an idle limit: execution fails only after that many seconds without output, so code that keeps printing is never cut off, and `--timeout 0` means no limit. On 0.8.0/0.9.0 the old deadline was never enforced, so code that is silent for longer than `--timeout` now fails where it used to complete. `colab repl` and `colab install`, which pass no timeout, wait until execution finishes, or until the kernel dies or restarts. None of them busy-loop while waiting. `colab auth` and `colab drivemount` are unchanged. See #183.
---

# Design: Execution and Interactive Interaction (`repl`, `exec`, `console`)

## Overview
Execution involves sending Python code (or shell commands) to the Jupyter kernel running on the Colab VM and processing the stream of output messages.

Remote execution is independent of the CLI's monitoring connection: interrupting that connection does not cancel submitted code. Recovery must restore tracking of the original request and retrieve its output and final result without submitting the work again.

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
- **Timeout Configuration**: Exposes a `--timeout` flag (default 30s): the longest silence allowed before the CLI stops waiting. Raise it for long-running silent tasks such as model compilation or data downloading. A timeout does not stop remote code or establish that it failed. `0` disables this idle limit, not the connection recovery limits.
- **Connection Recovery**: Enable WebSocket reconnection with a one-second interval, retaining the original kernel and Jupyter session. If the socket reconnect worker stops, continue reading the original journal over HTTP. Failed record reads trigger a bounded GET of assignments, at most once every five seconds, to refresh the original endpoint's proxy URL and token. Never select another assignment or allocate a replacement. Rebuild a stopped socket, or one with changed credentials, before the next submission; retain the Jupyter message counter so rebuilt clients cannot reuse old request IDs. Journal setup may be retried before user submission; an uncertain user-code send is never retried. Continuous unavailable journal reads, missing completion, or an idle/unreachable original kernel with incomplete records have 30-second recovery limits. The execution idle timeout can end waiting sooner; `--timeout 0` disables only that idle limit.
- **Recoverable Output and Completion**: Before submitting user code, build its request ID and install a journal for that exact ID in the kernel. Wait for setup acknowledgement before sending the original source, once, without wrapping it. This preserves IPython magics, top-level await, execution history, and the user namespace. Record stream output, rich display messages, errors, the shell reply, and idle status before transport delivery. After a disconnect, read the journal through the existing Contents API; reconcile it before accepting completion even when an interruption was too brief to observe in a socket-state poll. Validate contiguous sequence numbers and the parent request, and deduplicate live/replayed messages by their Jupyter message IDs. An execution that finishes during the WebSocket interruption can still return its actual result and output if the journal remains accessible.
- **Journal Lifecycle**: Journals use private files in `/content/.colab-cli-executions/`. The kernel restores its send method when all recorded requests have both replied and become idle. The CLI deletes a record after confirmed completion. An incomplete request keeps its record for diagnosis; subsequent setup removes inactive records older than one day. Recording adds a setup round trip and file writes; recovery reads complete file snapshots, parsing only newly appended records. No daemon or background worker is added.
- **Unknown Outcomes**: If recovery cannot establish the result, records are corrupt, submission is uncertain, or the idle limit expires, report uncertainty without resubmitting code. `exec` retains an `unknown:` running marker and `run` also skips teardown. Preserve recovery identity even when the original assignment disappears. A proxy's 401/403/404 is not proof that the VM is gone; live Colab probes return 404 for both an invalid proxy token and a missing kernel. A destroyed kernel, deleted journal, revoked account access, or prolonged network outage cannot be treated as recovered work.
- **Resume (`colab resume`)**: Save `{request_id, path}` in `SessionState.execution` before sending user code, and clear it only after confirmed completion. Existing state without this optional field remains readable. A new CLI process reads saved records through HTTP without constructing a kernel client or sending execution requests. Replay starts at the beginning, so a previous process's displayed output may repeat, while user code is never repeated. `--timeout` and `--output-image` work as in `exec`. Resume reports a remote error via a nonzero exit status and retains the runtime even on success. For notebooks it recovers only the interrupted cell's transcript, without executing later cells or updating the local notebook. New `exec`/`run` submissions cannot overwrite an unresolved identity. No background monitor is created.

### 3. Console (`colab console`)
- **Implementation**: Connects directly to the backend terminal endpoint (`/colab/tty`) via WebSockets using `websocket-client`.
- **Interactive**: Bypasses the Jupyter kernel entirely to provide a raw, PTY-backed bash session on the Colab VM.
- **Terminal Management**: Configures `sys.stdin` to raw mode using `termios` and `tty`, passing single characters to the socket and writing raw ANSI escape sequences directly to `sys.stdout.buffer`. Hooks into `SIGWINCH` to communicate local terminal dimensions (`cols`/`rows`) to the remote bash environment so output rendering works perfectly during resizing.
- **Piped stdin**: Detected via `sys.stdin.isatty()`. When piped, the input characters are forwarded one at a time to the remote pty, and on EOF the client sends `exit\n` and then closes the websocket itself after `PIPED_EOF_GRACE_SECONDS` (0.5s) so the user's shell goodbye text drains back. The remote `/colab/tty` endpoint wraps bash in tmux, which intercepts a bare `\x04` as a literal character — that is why we send `exit\n` rather than Ctrl-D.

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
- **Test Cases**: Reconnect while waiting for output or a reply; recover success/error and missing output; refresh a rejected proxy without resubmission; continue HTTP tracking after the socket worker exits; persist/reload identity before send; resume without a kernel client; reject overwriting unresolved work; reject corrupt records; bound unavailable-record and missing-completion waits with `--timeout 0`. Disk-recording failures must not suppress live output. Run `uv run pytest tests/test_execution_recovery.py tests/test_execution_journal.py tests/test_streaming.py tests/test_runtime.py tests/test_exec.py tests/test_run.py`.
- **Live Test**: `uv run bash integration/repro_execution_reconnect/test.sh` verifies two real TCP interruptions, success and failure during disconnection, stdout/stderr and rich display replay, top-level await and shell magic semantics, exactly-once side effects, unchanged kernel/session IDs, unknown-result runtime preservation, and normal `run` cleanup. State is isolated and all owned endpoints are released in `finally`.

### 2. TTY and Piping
- **Test Case**: Mock `sys.stdin.isatty()` to verify `colab repl` correctly switches between interactive mode and one-shot piped execution.
- **Test Case**: Verify large piped inputs are handled without buffer overflow or truncation.
- **Test Case**: `colab console` with piped stdin sends `exit\n` and calls `ws.close()` on EOF (regression: previously sent `\x04` only and hung).
- **Test Case**: `colab console` in TTY mode does not synthesize an exit on EOF (the user owns the session lifecycle).
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
