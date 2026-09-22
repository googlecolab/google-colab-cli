---
log:
2026-08-15: Fixed long-running Console output freezes caused by the raw `/colab/tty` flow-control protocol. The runtime requests an application-level acknowledgement about every 100 KB and pauses its PTY after six unacknowledged chunks; Console now acknowledges each request after flushing the corresponding terminal output. Protocol ping/pong remains transport-only liveness and is not treated as PTY progress. Added a CPU-only live regression that streams beyond the former 600 KB stall threshold without touching pre-existing assignments.
2026-08-14: Prevented Console reconnects from appearing frozen after a concurrent `colab stop` or local-proxy failure. Reconnect now treats a removed local binding as conclusive before calling the control plane, bounds its assignments refresh to ten seconds, continues with the last runtime token after transient refresh errors, and makes raw-mode Ctrl-C cancellation explicit. The CPU-only live regression covers both proxy-failure fallback and cross-process binding removal without touching pre-existing assignments.
2026-08-14: Tightened Console reconnect semantics after review. Shell `exit`/`logout`/Ctrl-D input now suppresses reconnect only when the peer closes within two seconds, so leaving a nested shell or foreground program cannot permanently disable later recovery. A connection that remains healthy for 30 seconds resets the 1/2/5/10/30-second backoff, and every independent loss reports its reason. The CPU-only live regressions now use pipeline failure propagation and recover the exact test endpoint during EXIT cleanup, closing the assignment-leak window between successful creation and endpoint capture.
2026-08-13: Made `colab console` resilient to long-lived proxy disconnects. Console now sends protocol ping/pong heartbeats, reports connection loss and retry progress, refreshes runtime-proxy credentials before reconnecting to the same endpoint, and reuses one stoppable stdin forwarding thread across attempts. Interactive sessions retry with 1/2/5/10/30-second backoff (then every 30 seconds); piped input is never replayed. Normal shell exits remain terminal, while an endpoint deletion or same-name replacement stops reconnecting safely. Added bounded loopback and resource-retention regressions after an unbounded zero-delay test loop caused pytest output capture to exhaust local memory.
2026-08-13: Fixed issue #106 for `exec`, `repl`, `console`, and `restart-kernel`: runtime startup proactively uses refreshed proxy credentials and retries one proxy-auth failure after a control-plane refresh. Terminal failures no longer unconditionally prune local state, and endpoint-guarded metadata cleanup prevents stale `finally` blocks from restoring expired tokens or reviving removed sessions.
2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' | colab console -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' | colab console -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.
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
- **Timeout Configuration**: Exposes a `--timeout` flag (default 30s) to allow long-running silent tasks (like model compilation or data downloading) to execute without being prematurely killed.

### 3. Console (`colab console`)
- **Implementation**: Connects directly to the backend terminal endpoint (`/colab/tty`) via WebSockets using `websocket-client`.
- **Interactive**: Bypasses the Jupyter kernel entirely to provide a raw, PTY-backed bash session on the Colab VM.
- **Terminal Management**: Configures `sys.stdin` to raw mode using `termios` and `tty`, passing single characters to the socket and writing raw ANSI escape sequences directly to `sys.stdout.buffer`. Hooks into `SIGWINCH` to communicate local terminal dimensions (`cols`/`rows`) to the remote bash environment so output rendering works perfectly during resizing.
- **Liveness detection**: `websocket-client` sends a protocol ping every 20 seconds and requires a pong within 10 seconds. This both keeps an otherwise-idle proxy path active and turns a silently dead path into a detectable disconnect.
- **PTY output flow control**: The runtime marks roughly every 100 KB of terminal output with `"ack": true` and pauses the PTY after six unacknowledged chunks. Console writes and flushes each marked chunk before replying `{"ack": true}` on the same WebSocket. Protocol pong frames prove only transport liveness and do not satisfy this application-level acknowledgement.
- **Interactive reconnects**: An abnormal close after a successful handshake is reported on stderr, then retried after 1, 2, 5, 10, and 30 seconds, followed by 30-second retries until the user presses Ctrl-C or the assignment is confirmed gone. A connection healthy for 30 seconds resets this backoff so an unrelated later outage starts again at one second. Each retry first checks that the original local binding still exists, then refreshes its assignment credentials with a ten-second HTTP timeout; a transient refresh failure falls back to the last token, while a removed binding or same-name replacement stops safely. One stdin-forwarding thread is reused across every attempt and is stopped when Console exits, so reconnects cannot accumulate competing terminal readers.
- **Close semantics**: Close codes 1000 (normal) and 1001 (going away) end Console without reconnecting. A locally recognized `exit`, `logout`, or Ctrl-D request suppresses an abnormal-close reconnect for two seconds; this bounded hint covers immediate shell termination without permanently disabling recovery when the input only leaves a nested shell or foreground program. A 401/404 initial-handshake error is returned to the shared one-time credential-refresh path rather than entering the unbounded transport reconnect loop.
- **Status visibility**: Connection loss, every retry delay/attempt, successful reconnection, raw-mode Ctrl-C cancellation, and final closure are printed as concise `[colab]` messages on stderr, separate from the remote terminal byte stream.
- **Piped stdin**: Detected via `sys.stdin.isatty()`. When piped, the input characters are forwarded one at a time to the remote pty, and on EOF the client sends `exit\n` and then closes the websocket itself after `PIPED_EOF_GRACE_SECONDS` (0.5s) so the user's shell goodbye text drains back. The remote `/colab/tty` endpoint wraps bash in tmux, which intercepts a bare `\x04` as a literal character — that is why we send `exit\n` rather than Ctrl-D.
- **Piped disconnects**: Piped input is never reconnected or replayed because the CLI cannot know which bytes the remote shell already consumed. An abnormal close returns a non-zero exit with a concise error instead.

### 4. Expired Runtime-Proxy Credentials

- Session resolution adopts the latest runtime-proxy token and URL returned by `/tun/m/assignments` before Jupyter or terminal connection startup.
- A proxy-auth 401/404 triggers one refresh-and-retry. A repeated failure is reported without deleting the local binding unless the assignments endpoint independently confirms the VM endpoint is gone.
- Kernel/session ID callbacks and `running`/`last_execution` cleanup use endpoint-guarded field updates, so a stale command cannot overwrite a token refreshed by another invocation or recreate a deleted session. This specifically prevents the former Console `finally` resurrection path.

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
- **Test Case**: `colab console` with piped stdin sends `exit\n` and calls `ws.close()` on EOF (regression: previously sent `\x04` only and hung).
- **Test Case**: `colab console` in TTY mode does not synthesize an exit on EOF (the user owns the session lifecycle).
- **Test Case**: An abnormal interactive close prints visible state changes, refreshes credentials for the same endpoint, and reconnects using one stdin reader; deletion and same-name endpoint replacement stop retries.
- **Test Case**: Protocol ping/pong settings are enabled, normal closes do not reconnect, and initial 401/404 handshake failures return to the bounded shared token-refresh path.
- **Test Case**: A terminal message marked `"ack": true` is flushed before Console replies with `{"ack": true}`; unmarked messages do not emit acknowledgements. A live isolated CPU Console streams beyond 600 KB and still receives a trailing sentinel.
- **Test Case**: A real loopback WebSocket closes once abnormally and then normally, proving that the refreshed token is used on the second handshake. The fake peer has a hard timeout and propagates server-thread exceptions. `integration/repro_console_reconnect/test.sh` repeats the fault injection against an isolated live CPU assignment, injects one control-plane `ProxyError`, removes a copied binding from another process, and verifies fallback/local-stop status without changing the pre-existing endpoint snapshot.
- **Test Case**: Test-only reconnect limits prevent zero-delay retry fixtures from spinning forever. A 500-attempt object-retention regression verifies that old WebSocket attempts are collectable, and an idle-stdin regression verifies that the forwarding thread stops without waiting for another keystroke.
- **Test Case**: Piped disconnects fail once without retrying or replaying input.
- **Test Case**: Shell-exit intent expires after two seconds, short-lived reconnect failures continue their backoff, and a connection healthy for 30 seconds resets the next outage to attempt one.
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
- **Test Case**: Runtime-proxy 401/404 startup failures refresh and retry once without unconditional pruning.
- **Test Case**: Console and execution cleanup merge metadata into the latest endpoint-matching state and never revive a removed session.
