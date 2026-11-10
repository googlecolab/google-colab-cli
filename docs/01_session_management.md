---
log:
2026-11-05: Without `-s`, `State.resolve_session` skips attached records and refuses, naming `colab ssh -s NAME` and `colab stop -s NAME`, when only attached records remain.
2026-11-05: Attached sessions are ssh-only: `State.get_session` refuses an attached record before any token refresh unless the caller (`colab ssh`, `run` cleanup) allows it, and `colab url` refuses one too.
2026-11-05: The CLI never unassigns a runtime this store records as attached: `colab stop` and `colab ssh --rm` forget an attached-only record without stopping it, `--rm` refuses a name in both files, `Client.unassign` refuses attached endpoints, and `run` cleanup removes only its own CLI record once its runtime is released. `new -s` / `run -s` refuse an attached or duplicated name.
2026-11-04: `StateStore` keeps attached sessions in their own owner-only file beside the sessions file; a name in both files fails with an error that names `colab stop -s NAME`, which forgets the attached record without unassigning. A sessions path that names an attached file, or whose attached path links to another kind of file, is refused (hard links and copies are not detected).
2026-11-04: `--gpu` and `--tpu` now reject unknown values instead of silently selecting `A100` (GPU) or `v6e1` (TPU). `resolve_runtime_options()` uses the new `GPU_ACCELERATORS` / `TPU_ACCELERATORS` maps and raises a `typer.BadParameter` that names the flag and the supported list; `--help` is generated from the same maps so it cannot drift. Validation runs before any assignment request, in both `colab new` and `colab run`.
2026-11-02: Added `_check_linux_keyring_locked` in `auth.py` to check if the active Linux `SecretService` keyring collection is locked via `secretstorage` before reading or writing OAuth tokens, logging and printing an immediate warning with the collection label, timeout, and `--auth=adc` hint while still attempting the keyring operation.
2026-10-29: The debug log (`colab.log` and `--logtostderr`) masks access tokens, runtime proxy tokens, OAuth codes, client secrets, auth headers and cookies, including in percent-encoded URLs; `jupyter_kernel_client` logs at INFO so kernel stdin replies are not recorded.
2026-10-29: `sessions.json`, `settings.json`, `colab.log` and session history files are created with mode 0600, and existing copies are tightened to 0600 the next time the CLI uses them (history files whenever the history logger starts).
2026-10-21: Stored OAuth2 credentials in the OS native keyring via the `keyring` library instead of `~/.config/colab-cli/token.json`, falling back to `_run_remote_flow` if keyring is inaccessible. Keyring failures are reported on stderr, and an existing `token.json` is migrated into the keyring and then deleted (or deleted outright if the keyring already has a token).
2026-10-14: Added configurable HTTP request timeouts and bounded allocation retries. Uncertain allocation POSTs are reconciled by GET using the same notebook hash before resubmission.
2026-09-25: Runtime proxy tokens are now refreshed before expiry. `SessionState` persists `token_expires_at`, and `State.get_session` re-fetches the token via `GET /tun/m/assignments` when it's within 5 minutes of expiring.
2026-09-25: Removed the keep-alive background daemon and related client/CLI ping functions. VM liveness is automatically maintained by the Colab backend based on kernel activity.
2026-08-10: Added `colab usage` for account-level compute-unit rate/balance via `GET /tun/m/ccu-info` on the session backend (same bearer token as `colab new`).
2026-08-09: Added `--high-mem` to `colab new`, `colab run`, and `colab ssh` (auto-create). Assign requests now send `shape=hm` when high-RAM is requested; `colab sessions` and `colab status` display machine shape.
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
    - `G4`: NVIDIA G4 (entry-level datacenter GPU).
    - `A100`: NVIDIA A100 (high-performance GPU).
    - `H100`: NVIDIA H100 (latest-gen performance GPU).
- **TPU Accelerators**:
    - `V2-8`: TPU v2 (8 cores).
    - `V5E1`: TPU v5e (1 core, optimized for inference/efficient training).
    - `V6E1`: TPU v6e (1 core, high performance).

### 3. Machine shape (`shape`)
Defines the RAM profile for runtimes that support a choice (CPU, T4, A100, etc.).
- `STANDARD` (default): omit the `shape` query param on assign.
- `HIGH_RAM`: send `shape=hm` on assign (requires Colab Pro/Pro+ entitlement).

Accelerators with only one shape (L4, v5e1, v6e1) ignore `--high-mem`.

### 4. CLI Mapping
The CLI maps user flags to these backend parameters:
- `colab new my-session` -> `variant=DEFAULT`, `accelerator=NONE`
- `colab new my-session --gpu=L4` -> `variant=GPU`, `accelerator=L4`
- `colab new my-session --tpu=v5e1` -> `variant=TPU`, `accelerator=V5E1`
- `colab new my-session --high-mem` -> adds `shape=hm` (when supported)
- `colab new my-session --gpu A100 --high-mem` -> `variant=GPU`, `accelerator=A100`, `shape=hm`

`--gpu` accepts `T4`, `L4`, `G4`, `H100`, and `A100`; `--tpu` accepts `v5e1`
and `v6e1` (case-insensitive). Anything else is rejected with the supported
list before a VM is requested. Unknown values used to fall back silently to
`A100`/`v6e1`, which turned a typo in an automation script into a real,
billable allocation.

## Approach

### HTTP Timeout and Allocation Recovery

`COLAB_CLI_REQUEST_TIMEOUT` sets the HTTP request timeout in seconds. If unset,
the session's existing default applies (AuthorizedSession defaults to 120 seconds).
An explicit `timeout=` passed by a caller takes precedence. This does not change
kernel execution timeouts, Colab runtime lifetime, or benchmark job timeouts.

Allocation POSTs retry `ReadTimeout` and HTTP 502/503/504 at most
`COLAB_CLI_ASSIGN_RETRIES` times (default 3; 0 disables retries). Between attempts,
the client waits `COLAB_CLI_RETRY_BACKOFF * 2**attempt` seconds (default 10, 20, 40).
It then queries the same notebook hash: an existing allocation is returned directly;
otherwise the refreshed XSRF token is used for the next POST. HTTP 412 still raises
`TooManyAssignmentsError`; other errors are not retried. Failed initial/recovery
GETs and exhausted POST retries propagate to the caller. There is no unlimited wait.

```bash
COLAB_CLI_REQUEST_TIMEOUT=300 COLAB_CLI_ASSIGN_RETRIES=3 COLAB_CLI_RETRY_BACKOFF=10 \
  colab new --session kernel-benchmark-v6e --tpu v6e1
```

Regression tests cover timeout precedence, same-request recovery, refreshed
tokens, retry limits, exponential backoff and permanent-error handling. A live
allocation/recovery test is also required before declaring the behavior verified
end to end; mocked timeouts alone do not establish backend compatibility.

### 1. New Session (`colab new`)
- **API**: `GET https://colab.sandbox.google.com/tun/m/assign` (based on HAR).
- **Parameters**:
    - `nbh`: Notebook hash. Generated from a unique UUID per CLI session/client instance, transformed to web-safe base64 with specific padding (44 characters total).
    - `nsa`: 1 (Standard flag observed in browser traces, typically for "next-gen session architecture").
    - `variant`: Selected from the list above.
    - `accelerator`: Selected from the list above.
- **State Persistence**: The response contains a `token` and potentially a backend URL or identifier. We will store this in a local JSON file (default `~/.config/colab-cli/sessions.json`), readable only by its owner (mode 0600).
    - Format: `{ "session_name": { "token": "...", "backend_url": "...", "hardware": "..." } }`

### 2. Session Status (`colab status`)
- **Session lines**: Local metadata per tracked session — hardware, shape, variant, IDLE/BUSY (from CLI bookkeeping), and optional last-execution detail. Syncs with `GET /tun/m/assignments` to prune stale entries.
- **Future**: Real-time VM resource usage (CPU/RAM/GPU) via a kernel diagnostic snippet is still TODO. For account-level compute-unit balance and usage rate, use `colab usage`.

### 3. Stop Session (`colab stop`)
- **Attached sessions**: `colab stop` on an attached record only forgets it (no kernel shutdown, no unassign) and says the runtime keeps running. `Client.unassign` refuses an endpoint the store holds as attached, `colab ssh --rm` reports a name in both files instead of forgetting the attached copy, and `run` cleanup removes only its own CLI record, once its runtime is released (otherwise it keeps the record and names `colab stop -s NAME`). `colab new -s` / `colab run -s` refuse an attached or duplicated name before any request.
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

### 5. Session Liveness
VM liveness is automatically maintained by the Colab backend based on active kernel executions and interactions. The CLI runs no keep-alive daemon. A session persists until explicitly terminated via `colab stop` or until the backend reclaims it due to inactivity.

### 6. Runtime Proxy Token Refresh
The runtime proxy token (`runtimeProxyInfo.token`) authenticates every request to the VM and expires after `tokenExpiresInSeconds` (1 hour). `SessionState.token_expires_at` stores the absolute expiry, computed when the token is received. Commands that talk to the VM load their session through `State.get_session(name)`, which, when the token is missing an expiry or is within 5 minutes of it, calls `GET /tun/m/assignments`, takes the fresh token/URL for the matching endpoint, and persists it. If the endpoint is no longer assigned, the session is pruned. `sync_sessions` also writes back the fresh tokens it already fetches. Long-lived connections (`repl`, `console`, `ssh`) only present the token on initial connect and aren't refreshed mid-session.

## TODO / Future Work
- **Backend Sync**: Implement a way to reconcile the local `sessions.json` with the output of `colab sessions`.
- **VM Resource Usage**: Add real-time resource usage (CPU/RAM/GPU) to the `status` output by executing a diagnostic snippet on the VM (distinct from account-level CU info, which is available via `colab usage`).

## Implementation Details
- **Authentication**: Uses `google-auth-oauthlib` to perform a local server OAuth flow.
- **Global Flags**:
    - `-c`, `--client-oauth-config`: Path to the client secrets JSON file (default: `~/.colab-cli-oauth-config.json`).
    - `--config`: Path to the session state JSON file (default: `~/.config/colab-cli/sessions.json`). Attached sessions and their tokens are kept beside it in `<stem>.attached.json`, where `<stem>` is the path without its extension; a path ending in `.attached.json` is refused.
- **Token Storage**: Credentials are persisted to the OS native keyring via the `keyring` library (`service_name="google-colab-cli"`, `username="oauth_token"`) after the initial flow. If the keyring is inaccessible or locked, `_run_remote_flow` is invoked for the current run and a warning is printed to stderr. A legacy `~/.config/colab-cli/token.json` is migrated into the keyring on first use and deleted after a successful keyring write, or immediately if the keyring already holds a token.
- Use `requests` for robust HTTP interactions and `pydantic` for schema validation.
- Handle authentication headers (likely `Authorization: Bearer <token>` or cookies).

### State Persistence & File Locking
- **Stores**: `StateStore` (`sessions.json`) and `SettingsStore` (`settings.json`) both derive from `_LockedFileStore`, which guards concurrent access across independent `colab` invocations.
- **Attached sessions**: `StateStore` keeps records with `attached=True` (a runtime the CLI did not create) in a second locked, owner-only file derived from the sessions path (`sessions.attached.json`, or `<stem>.attached.json` for `--config <stem>.json`), so versions that read only `sessions.json` never see them. `add` routes on `attached`, `get` and `list` read both files, and `remove` removes from both. The file a record is read from decides its `attached` flag, so a refresh writes each record back to its own file. A name present in both raises `DuplicateSessionError` from `get` and `list`, so commands that read the store stop before any Colab request; `colab stop -s NAME` recovers by forgetting the attached record without unassigning. A sessions path ending in `.attached.json` (in any case, or a symlink to one), or one whose derived attached path is a symlink to a file not named `*.attached.json`, raises `AttachedStorePathError`. The CLI applies this check to `--config` at startup, before any command runs. Hard links and renamed copies are not detected in either direction: do not hard-link or copy an attached file or a sessions file under another store's name.
- **Ssh-only**: `State.get_session` refuses an attached record, pointing to `colab ssh -s NAME`, before any token refresh unless the caller passes `allow_attached=True`, which only `colab ssh` and `run` cleanup do. `colab url` refuses one as well, because opening the runtime in a browser would start a kernel on it. `colab sessions` and `colab status` still list attached records. Without `-s`, `State.resolve_session` skips attached records; when only attached records remain, before or after stale CLI records are pruned, it refuses and names `colab ssh -s NAME` and `colab stop -s NAME`. Bare `colab ssh` may pick an attached session, but with one CLI and one attached record it refuses as ambiguous.
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
- **Test Case**: Verify `--config` override correctly directs all operations to the specified file path and its `<stem>.attached.json`, and refuses a path ending in `.attached.json`.
- **Test Case (cross-platform locking)**: Verify the store locks via `filelock.ReadWriteLock` on the `<path>.lock` sidecar and does not import the POSIX-only `fcntl`.
- **Test Case (shared/exclusive semantics)**: Verify reads go through `read_lock()` and writes through `write_lock()`.
- **Test Case (cross-process exclusion)**: Hold the write lock from a separate process and confirm the store's in-process write blocks until release.
- **Test Case (concurrent readers)**: Hold a read lock from a separate process and confirm the store can still complete a read concurrently.
- **Test Case (multi-thread regression)**: Two `StateStore` instances writing from different threads must serialize without raising `RuntimeError` (guards the `is_singleton=False` choice).
