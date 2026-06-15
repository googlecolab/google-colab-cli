---
log:
2026-06-15: Switched the keep-alive daemon from the `colab.pa.googleapis.com` `RuntimeService/KeepAliveAssignment` RPC to a Tunnel Frontend HTTP ping (`GET /tun/m/<endpoint>/keep-alive/` with `X-Colab-Tunnel: Google`) on `colab.research.google.com`. The RPC required `serviceusage` consumer access to Colab's internal project `1014160490159`, which ordinary user accounts lack, so every external user hit HTTP 403 `USER_PROJECT_DENIED` and their CLI sessions were idle-pruned within minutes (issue #14). Reproduced live with a third-party account; verified the tunnel ping is accepted by the same bearer-token credential that already works for `assign`. A `ReadTimeout` on the ping is treated as success (TFE records activity before forwarding to the often-non-responding VM). Generalized the pre-flight remediation messaging away from the now-irrelevant `colaboratory`/`pa.googleapis.com` framing, and removed the dead grpc-web client-registry/API-key code.
2026-06-10: Replaced the POSIX-only `fcntl.flock` file locking in `_LockedFileStore` with the cross-platform `filelock` library (reported broken on Windows). Reads use `ReadWriteLock.read_lock()` (shared) and writes use `write_lock()` (exclusive), preserving the original `LOCK_SH`/`LOCK_EX` semantics. The lock is constructed with `is_singleton=False` so two `StateStore` instances for the same path in one process don't collapse into a single reentrant lock (which would raise `RuntimeError` on multi-threaded write contention). Added shared-read, cross-process exclusion, and multi-thread/multi-process regression tests.
---

# Design: Session Management (`new`, `status`, `stop`, `sessions`)

## Overview
Session management involves interacting with the Colab backend to allocate, monitor, and terminate runtimes.

## Runtime Parameters

The `colab new` command supports selecting specific hardware and runtime environments. Based on the `tpu-v5e1.har` trace and `colab-agent` source code, the following parameters and values are identified:

### 1. Variants (`variant`)
Defines the general class of hardware requested.
- `DEFAULT`: Standard CPU-based runtime.
- `GPU`: Request a GPU-accelerated runtime.
- `TPU`: Request a TPU-accelerated runtime.

### 2. Accelerators (`accelerator`)
Defines the specific hardware model.
- **None**: For `DEFAULT` variant.
- **GPU Accelerators**:
    - `T4`: NVIDIA T4 (standard free-tier GPU).
    - `L4`: NVIDIA L4 (cost-effective modern GPU).
    - `A100`: NVIDIA A100 (high-performance GPU).
    - `H100`: NVIDIA H100 (latest-gen performance GPU).
- **TPU Accelerators**:
    - `V2-8`: TPU v2 (8 cores).
    - `V5E1`: TPU v5e (1 core, optimized for inference/efficient training).
    - `V6E1`: TPU v6e (1 core, high performance).

### 3. CLI Mapping
The CLI maps user flags to these backend parameters:
- `colab new my-session` -> `variant=DEFAULT`, `accelerator=NONE`
- `colab new my-session -gpu=L4` -> `variant=GPU`, `accelerator=L4`
- `colab new my-session -tpu=v5e1` -> `variant=TPU`, `accelerator=V5E1`

## Approach

### 1. New Session (`colab new`)
- **API**: `GET https://colab.sandbox.google.com/tun/m/assign` (based on HAR).
- **Parameters**:
    - `nbh`: Notebook hash. Generated from a unique UUID per CLI session/client instance, transformed to web-safe base64 with specific padding (44 characters total).
    - `nsa`: 1 (Standard flag observed in browser traces, typically for "next-gen session architecture").
    - `variant`: Selected from the list above.
    - `accelerator`: Selected from the list above.
- **State Persistence**: The response contains a `token` and potentially a backend URL or identifier. We will store this in a local JSON file (default `~/.config/colab-cli/sessions.json`).
    - Format: `{ "session_name": { "token": "...", "backend_url": "...", "hardware": "..." } }`

### 2. Session Status (`colab status`)
- **API**: `/api/sessions` or querying the kernel for resource usage via a special "status" message.
- **Metric Collection**: Execute a small snippet on the VM to get memory/CPU usage if the backend API doesn't provide it directly.

### 3. Stop Session (`colab stop`)
- **API**: `POST https://colab.sandbox.google.com/tun/m/unassign/<endpoint>` (based on `tpu-v5e1-unassign.har`).
- **Flow**:
    1.  Perform a `GET` request to the unassign URL to obtain a fresh XSRF token.
    2.  Perform a `POST` request to the same URL with the `X-Goog-Colab-Token` header.
- **Parameters**:
    - `authuser`: 0.
    - `<endpoint>`: The unique session identifier returned during assignment (e.g., `tpu-v5e1-s-kkb-...`).
- **Cleanup**: Remove the session from the local state file upon successful 204 response.

### 4. Session Listing (`colab sessions`)
- **API**: `GET https://colab.research.google.com/tun/m/assignments` (based on `colab-agent` implementation).
- **Function**: Lists all active VM assignments for the user. This is useful for synchronizing local state with actual backend sessions.

### 5. Keep-Alive Protocol
To prevent Colab VMs from being deleted due to idle timeouts (standard is ~90 minutes), the CLI implements a background keep-alive mechanism.
- **Daemon Process**: Since the CLI is a fire-and-forget tool, `colab new` spawns a detached background process running a hidden `keep-alive` command.
- **Tunnel ping**: Every 60 seconds, the daemon issues `GET https://colab.research.google.com/tun/m/<endpoint>/keep-alive/` with the header `X-Colab-Tunnel: Google`, authenticated with the user's own Gaia bearer token (the same credential and host used for `/tun/m/assign`). The Tunnel Frontend (TFE) records `LastActiveTime` before forwarding the request, which refreshes the idle timer. This matches the official `colab-vscode` extension's `sendKeepAlive`. TFE notes the activity on arrival and then forwards to the VM, which often does not answer on this path — so the request commonly read-times-out even though the keep-alive succeeded; a `ReadTimeout` is therefore treated as success, while genuine HTTP errors (e.g. 404 for a deleted assignment) propagate.
  - **Why not the RuntimeService RPC**: The previous implementation called `google.internal.colab.v1.RuntimeService/KeepAliveAssignment` at `colab.pa.googleapis.com` with `X-Goog-User-Project: 1014160490159`. That path requires the caller to be a `serviceusage` consumer of Colab's internal project `1014160490159`, which no ordinary user account is — so it returned HTTP 403 `USER_PROJECT_DENIED` for every external user, causing CLI sessions to be idle-pruned within minutes (issue #14). Dropping the header instead produced HTTP 400 `CONSUMER_INVALID` (public API-key project ≠ bearer-token quota project). The browser only succeeds because it rides the user's `google.com` cookie through an internal cookie-proxy (`colab.clients6.google.com`), which a headless bearer-token client cannot use. The TFE tunnel ping needs no project entitlement and works for any account that can assign a VM.
- **Pre-flight (`colab new`, OAuth2/ADC only)**: Immediately after a successful `assign`, the CLI invokes `keep_alive_assignment` once synchronously. If the response is 403 with a `SCOPE_NOT_PERMITTED` body, it unassigns the new VM (to avoid leaking a billable assignment) and prints a per-provider remediation message before exiting non-zero. Other errors are tolerated — the daemon will retry and surface them via the structured event log. (Because keep-alive now uses the same backend/credential as `assign`, a scope failure at this stage is rare — assignment would normally have failed first.)
- **Structured logging**: The daemon emits `keep_alive_started` (with `pid`, `endpoint`), one `keep_alive_error` per failed iteration (with `status_code`, `error_type`, truncated `error`, `response_body`, `iteration`, `consecutive_4xx`), and `keep_alive_stopped` (with `reason`, `iterations`, `duration_seconds`, optional `last_error`, optional `expected_endpoint`/`actual_endpoint`). All three are rendered specially by `colab log` so users get diagnostic context without parsing JSONL by hand.
- **Termination**:
    - **Explicit**: `colab stop` terminates the daemon using its stored PID.
    - **Implicit**: If a session is pruned (e.g., during `sync_sessions`), its daemon is also terminated.
    - **Safety Fallback**: The daemon automatically terminates after 24 hours to prevent permanent zombie processes.
    - **State Check**: The daemon periodically verifies that its session still exists in the local state store; if missing, it exits.
    - **Repeated 4xx**: After two consecutive 4xx responses, the daemon exits with `reason=consecutive_4xx_errors`. With the TFE tunnel ping, a normal read-timeout is not counted as a 4xx (it is treated as success), so this branch is now reached only by genuine HTTP errors such as a 404 for a deleted/expired assignment.

## TODO / Future Work
- **Backend Sync**: Implement a way to reconcile the local `sessions.json` with the output of `colab sessions`.
- **Resource Usage**: Add real-time resource usage (CPU/RAM/GPU) to the `status` output by executing a diagnostic snippet on the VM.

## Implementation Details
- **Authentication**: Uses `google-auth-oauthlib` to perform a local server OAuth flow.
- **Global Flags**:
    - `-c`, `--client-oauth-config`: Path to the client secrets JSON file (default: `~/.colab-cli-oauth-config.json`).
    - `--config`: Path to the session state JSON file (default: `~/.config/colab-cli/sessions.json`).
- **Token Storage**: Credentials are persisted to `~/.config/colab-cli/token.json` after the initial flow.
- Use `requests` for robust HTTP interactions and `pydantic` for schema validation.
- Handle authentication headers (likely `Authorization: Bearer <token>` or cookies).

### State Persistence & File Locking
- **Stores**: `StateStore` (`sessions.json`) and `SettingsStore` (`settings.json`) both derive from `_LockedFileStore`, which guards concurrent access across independent `colab` invocations and the detached keep-alive daemon.
- **Cross-platform locking**: Locking uses the [`filelock`](https://pypi.org/project/filelock/) library rather than `fcntl.flock`. `fcntl` is POSIX-only and is unavailable on Windows, so the original implementation crashed on import there. `filelock` provides the same advisory cross-process locking on Linux, macOS, and Windows.
- **Shared vs. exclusive**: Each store owns a `filelock.ReadWriteLock` bound to a sidecar file (`<path>.lock`). Reads acquire `read_lock()` (shared — multiple concurrent readers allowed) and writes acquire `write_lock()` (exclusive). This preserves the `LOCK_SH`/`LOCK_EX` distinction of the previous `fcntl` implementation.
- **`is_singleton=False`**: The `ReadWriteLock` is created with `is_singleton=False`. With `filelock`'s default (`True`), two `ReadWriteLock` objects for the same path *within a single process* are deduplicated into one reentrant lock; its reentrancy guard then raises `RuntimeError` when two threads each construct their own `StateStore` and contend for the write lock. Disabling the singleton registry makes each store's lock independent so they serialize via the underlying file lock instead.

## Testing Strategy
TDD is mandatory for all session management features.

### 1. Mock Assignment API
- **Test Case**: Verify `colab new` correctly parses a `PostAssignmentResponse` and stores it in the local `StateStore`.
- **Test Case**: Verify `colab stop` sends a `POST` request with the correct XSRF token to the unassign endpoint.
- **Test Case**: Verify that the path provided via `-c` is correctly passed to the authentication flow.
- **Mocking**: Use `unittest.mock` to intercept `requests.Session.request` and return simulated XSSI-prefixed JSON payloads matching the HAR traces.

### 2. State Store Validation
- **Test Case**: Verify `StateStore` correctly handles file locking and multiple concurrent reads/writes.
- **Test Case**: Verify `--config` override correctly directs all operations to the specified file path.
- **Test Case (cross-platform locking)**: Verify the store locks via `filelock.ReadWriteLock` on the `<path>.lock` sidecar and does not import the POSIX-only `fcntl`.
- **Test Case (shared/exclusive semantics)**: Verify reads go through `read_lock()` and writes through `write_lock()`.
- **Test Case (cross-process exclusion)**: Hold the write lock from a separate process and confirm the store's in-process write blocks until release.
- **Test Case (concurrent readers)**: Hold a read lock from a separate process and confirm the store can still complete a read concurrently.
- **Test Case (multi-thread regression)**: Two `StateStore` instances writing from different threads must serialize without raising `RuntimeError` (guards the `is_singleton=False` choice).
