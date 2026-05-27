---
log:
2026-05-27: Updated auto-update upgrade hint to recommend `pip install --upgrade google-colab-cli` instead of `colab`, aligning with the PyPI package name.
2026-05-27: `colab url` now emits BOTH the `?dbu=<urlencoded path>` query parameter (existing) AND a new `#datalabBackendUrl=<full URL>` hash fragment (new). Format: `https://<host>/notebooks/empty.ipynb?dbu=%2Ftun%2Fm%2F<endpoint>#datalabBackendUrl=<host>/tun/m/<endpoint>`. Why both: some Colab frontend code paths consult the hash fragment first and ignore `dbu` entirely, so the previously-emitted query-only form failed silently for those users (the frontend fell through to allocating a fresh VM via `/tun/m/assign`). The fragment value is a FULL URL with scheme + host (NOT just the path) and is emitted RAW (no URL encoding) because browsers don't decode the fragment before passing `location.hash` to page JS — Colab's parser calls `new URL(rawString)` directly. The fragment host always matches `--host` so Colab's same-origin enforcement on embedded backend URLs doesn't block the connection, and sandbox/dev users (`--host https://colab.sandbox.google.com`) get a sandbox fragment automatically. Three new test cases in `tests/test_url.py` cover the raw-encoding requirement (`%3A`/`%2F` must NOT appear in the fragment), the both-signals-present invariant, and `--open` propagating the fragment to `webbrowser.open()`. Integration-verified live against synthetic session state with three host shapes (default, sandbox, trailing-slash); all produced correctly-shaped URLs with no `//` artifacts.
2026-05-07: Added a developer-only `colab whoami` subcommand (hidden from `colab --help`). Mints an access token via the same `auth.get_credentials(...)` path the rest of the CLI uses (honoring the global `--auth=...` flag), refreshes the credentials, then queries `https://oauth2.googleapis.com/tokeninfo` to print the email, scopes, audience, and expiry of whatever the CLI is about to send. Built specifically to short-circuit the "why is my call to colab.pa.googleapis.com 403-ing" debugging loop — the answer is almost always "missing scope" or "wrong identity", both of which `whoami` makes immediately visible. Hidden via `app.command(hidden=True)`; reachable via `colab whoami` or `colab whoami --help`. Suppressed from the daily-update banner check (added to `_AUTO_UPDATE_SUPPRESSED` in `cli.py`) so the banner doesn't obscure the auth output.
2026-05-11: Removed the local-file update source (`update_file_path` setting and `_fetch_local` helper); `colab update` now consults PyPI only. Switched the default `update_url` to the canonical PyPI JSON API (`https://pypi.org/pypi/google-colab-cli/json`), which already exposes the `info.version` schema the auto-update subsystem expects. Re-added `colab update --install` as a public self-install path that runs `pip install -U google-colab-cli` against the current `sys.executable`; Linux-only (other platforms exit non-zero with an explanatory message), and a silent no-op when the cached `latest_version` is already at or below the current install.
2026-05-12: Added an optional `timeout=` parameter to `ColabRuntime.execute_code` that flows through to both the `execute()` and `execute_interactive()` branches. `colab auth` and `colab drivemount` now pass `timeout=600` (10 min) via a shared `INTERACTIVE_AUTOMATION_TIMEOUT_SEC` constant in `commands/automation.py`. Background: `jupyter_kernel_client` defaults to a 10s wall-clock timeout that is consumed even when the kernel is idle waiting on `input_request`. With the drivefs hook intercepting that request and prompting the user to OAuth in their browser, any user that takes >10s to click through (essentially everyone) hit `TimeoutError` and saw "drivemount failed" even though the mount had actually succeeded server-side. The fix is scoped narrowly to the two human-in-the-loop subcommands; non-interactive paths (`colab exec`, `colab run`, `colab install`, `colab repl --pipe`, `colab console --pipe`) keep the upstream default since they receive continuous iopub traffic that resets the practical inactivity ceiling.
---

# Design: Automation and Utility (`auth`, `install`, `log`, `pay`, `version`, `update`, `whoami`)

## Overview

These subcommands are implemented by executing Python code on the Colab VM,
managing local state, or inspecting the environment.

## Authentication Strategies (CLI Backend)

The CLI supports two authentication strategies for talking to the Colab
backend, selected via the global `--auth=<provider>` flag:

1.  **`oauth2`** (default): Standard public InstalledAppFlow via
    `google-auth-oauthlib`. Opens a browser for consent, caches the refresh
    token at `~/.config/colab-cli/token.json`. Requires a client OAuth
    config at `~/.colab-cli-oauth-config.json` or a path passed via
    `-c/--client-oauth-config`.
2.  **`adc`**: Application Default Credentials via `google.auth.default()`.
    Honors the standard ADC discovery chain
    (`GOOGLE_APPLICATION_CREDENTIALS`, `gcloud auth application-default
    login`, GCE/GKE metadata server). Useful when running the CLI from
    environments that already have ambient Google credentials.

The choices are encoded as the `AuthProvider` string-enum in `auth.py`. The
`get_credentials(config_path, provider)` entry point dispatches on this enum,
allowing the core `Client` to remain authentication-agnostic — it only sees a
`requests.AuthorizedSession`.

### Required Scopes

The CLI talks to two distinct backends, each with different scope demands:

-   `colab.research.google.com` (session assignment / unassignment /
    contents API): the `userinfo.email` scope is sufficient.
-   `colab.pa.googleapis.com` (`RuntimeService`, used by
    `KeepAliveAssignment`): **requires** the
    `https://www.googleapis.com/auth/colaboratory` scope. Without it, every
    request returns HTTP 403 with body `[7,"Request had insufficient
    authentication scopes.",...]` and a `DebugInfo` mentioning
    `SCOPE_NOT_PERMITTED`. (The frontend additionally requires
    `X-Goog-Api-Client` to contain `grpc-web` — see
    `01_session_management.md` §5.)

How each provider supplies the scope:

-   **`oauth2`**: `PUBLIC_SCOPES` already includes `colaboratory`, so the
    InstalledAppFlow consent screen lists it. Existing cached tokens at
    `~/.config/colab-cli/token.json` that were minted before this change must
    be deleted to trigger a fresh consent flow.
-   **`adc`**: `google.auth.default(scopes=PUBLIC_SCOPES)` is called, and for
    credential subclasses that support `with_scopes` (service accounts,
    GCE/GKE metadata, impersonated) we re-apply via `creds.with_scopes(...)`.
    User credentials from `gcloud auth application-default login` ignore the
    `scopes=` kwarg AND raise `NotImplementedError` on `with_scopes`; those
    users must explicitly re-authenticate:

    ```
    gcloud auth application-default login \
        --scopes=openid,\
    https://www.googleapis.com/auth/cloud-platform,\
    https://www.googleapis.com/auth/userinfo.email,\
    https://www.googleapis.com/auth/colaboratory
    ```

    `userinfo.email` is required for the session backend at
    `colab.research.google.com` (otherwise assign/unassign/sessions return
    HTTP 401); `colaboratory` is required for the `RuntimeService` at
    `colab.pa.googleapis.com` (otherwise keep-alive returns HTTP 403);
    `openid` and `cloud-platform` are mandated by `gcloud` itself
    (`gcloud auth application-default login` rejects scope lists that
    omit `cloud-platform` with `Invalid value for [--scopes]`).

`colab new` performs a one-shot keep-alive pre-flight after `assign`
succeeds so missing-scope failures surface immediately (with per-provider
remediation guidance) rather than silently after ~1 minute via the daemon.

## Approach

### 1. Authentication (`colab auth`)

-   **Action**: Execute code on the VM to trigger user-interactive
    authentication using the classic Gcloud fallback.
-   **Code**: `python import os os.environ['USE_AUTH_EPHEM'] = '0' from
    google.colab import auth auth.authenticate_user()`
-   **Handling**: Setting `USE_AUTH_EPHEM` to `'0'` forces the kernel to print a
    standard `gcloud` verification URL and trigger an `input_request` message on
    the `iopub` channel. The CLI intercepts this via a `stdin_hook` and prompts
    the user locally, returning the code to unlock the kernel.

### 2. Package Installation (`colab install`)

-   **Action**: Execute `pip` on the VM.
-   **Code**: `python import sys, subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "..."])`
-   **Requirements File**: Upload `requirements.txt` if provided with `-r` and
    then run `pip install -r`.

### 3. Drive Mounting (`colab drivemount`)

-   **Action**: Execute `drive.mount()` and transparently proxy Colab's
    proprietary credential propagation flow.
-   **Code**: `python from google.colab import drive
    drive.mount('/content/drive')`
-   **Handling**: Because `drivefs` enforces the ephemeral side-channel
    propagation (`colab_request` over websocket), the CLI intercepts these
    messages using `ColabRuntime.colab_request_hook`. When intercepted, the CLI
    automatically interacts with the Colab backend
    (`/tun/m/credentials-propagation/`), prompts the user with the Google OAuth
    consent URL if needed, and dispatches the required `colab_reply` message to
    the `stdin` channel to unlock the kernel thread.
-   **Timeout**: The kernel is silent (no iopub traffic) the entire time the
    user is OAuthing in their browser. To avoid the upstream 10s
    `jupyter_kernel_client` default raising `TimeoutError` mid-flow, this
    subcommand passes `timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC` (600s) to
    `ColabRuntime.execute_code`. Same applies to `colab auth`.

### 4. Logging and Notebook Capture (`colab log`)

-   **Action**: Capture the session's command history and outputs.
-   **Storage**: Maintain a local JSON-L file of all major operations,
    executions, and stdin interactions in
    `~/.config/colab-cli/history/<session_name>.jsonl`.
-   **Viewing**: `colab log list` and `colab log show <session>`.
-   **Conversion (Planned)**: Future expansion to convert history logs to
    `.ipynb` or `.html`.

### 5. Subscription Management (`colab pay`)

-   **Action**: Open the Colab signup page in the user's browser.
-   **Implementation**: Uses
    `webbrowser.open("https://colab.research.google.com/signup")`.

### 6. Version Information (`colab version`)

-   **Action**: Show the current version of the Colab CLI.
-   **Implementation**:
    -   Attempts to retrieve the version using
        `importlib.metadata.version("colab")`.
    -   If not installed (e.g., running from source), it falls back to the short
        Git commit hash using `git rev-parse --short HEAD`.
    -   Dynamic versioning is supported in the build system via `hatch-vcs`.

### 7. Auto-Update (`colab update`)

-   **Action**: Check if a new version of the Colab CLI is available.
-   **Auto-check**: The CLI automatically checks for updates once every 24 hours
    during the execution of any command. Independently, the cached
    `latest_version` (see below) is consulted on **every** invocation so the
    upgrade banner remains visible between fetches without requiring a network
    round-trip.
-   **Suppressed subcommands**: To keep machine-parseable output clean, the
    daily fetch and the cached banner are suppressed for `update` (which
    runs its own check), `version`, `log`, `pay`, `url`, `help`, and
    `whoami`. The list lives as `_AUTO_UPDATE_SUPPRESSED` in the global
    Typer callback in `cli.py`.
-   **Manual-check**: `colab update` forces a check and prints the status.
-   **Implementation**:
    -   Fetches a PyPI-style JSON document from a configurable `update_url`
        (default: `https://pypi.org/pypi/google-colab-cli/json`) and reads
        `info.version`.
    -   Compares the fetched version with the current CLI version using
        PEP 440 / semantic versioning, falling back to string equality when a
        version is unparseable.
    -   Persists the following fields in `~/.config/colab-cli/settings.json`:
        -   `update_url`: source configuration.
        -   `last_check`: timestamp of the last fetch (drives the daily
            throttle).
        -   `enable_update_check`: master switch for both the daily fetch and
            the cached banner.
        -   `latest_version`: highest version observed during the most
            recent successful check. Updated whenever a strictly-newer
            version is observed (never downgraded), and preserved verbatim
            across failed checks so transient network issues do not erase
            the cache.
-   **Notification**: If a new version is found, a non-intrusive message is
    printed to the console with a `Run 'pip install --upgrade google-colab-cli' to
    update.` hint. The cached banner shown between fetches uses the generic
    `Run 'colab update' to update.` hint.
-   **Self-install (`--install`)**: An opt-in `--install` flag (default
    `False`) makes `colab update` shell out to `pip install -U
    google-colab-cli` (using `sys.executable` so the upgrade lands in the
    same interpreter the CLI is running under). **Linux only**; on other
    platforms the command exits non-zero with an explanatory message. When
    the cached `latest_version` is already at or below the current install,
    the flag is a silent no-op so it is safe to wire into automation. If
    `pip` exits non-zero, `colab update --install` propagates the same
    exit code.

### 8. Identity Inspection (`colab whoami`) [developer-only]

-   **Action**: Resolve the active credentials, mint an access token, and
    print the email, audience, scopes, and expiry of that token.
-   **Visibility**: Registered with `hidden=True` so it does not appear in
    `colab --help`. Discoverable via source code, `colab whoami --help`, or
    word-of-mouth. The intent is to keep the public surface focused on
    end-user commands while still giving developers a one-shot debugging
    aid.
-   **Implementation**:
    -   Calls `auth.get_credentials(state.client_oauth_config,
        provider=state.auth_provider)` — the exact same code path the
        `Client` uses — so the token reflects what the rest of the CLI
        would actually send.
    -   Always calls `creds.refresh(Request())` before reading
        `creds.token`. Service-account, GCE/GKE-metadata, and some
        impersonated credentials lazy-mint the token; without an explicit
        refresh `creds.token` is `None` even for valid credentials.
    -   Hits `https://oauth2.googleapis.com/tokeninfo?access_token=<token>`
        via stdlib `urllib.request` rather than the already-authorized
        `requests.AuthorizedSession`. The tokeninfo endpoint accepts the
        token as a query parameter and does NOT want a `Bearer` header
        alongside it.
    -   Renders `expires_in` (seconds) as minutes for readability.
    -   On HTTP 4xx from tokeninfo (typical for revoked/expired tokens),
        the JSON error body is surfaced verbatim rather than being
        swallowed; the developer needs to see *why* the token was
        rejected.
-   **Output shape**:
    ```
    Auth provider: adc
    Email:         user@example.com
    Audience:      764086051850-...apps.googleusercontent.com
    Expires in:    47m
    Scopes:
      - email
      - https://www.googleapis.com/auth/cloud-platform
      - https://www.googleapis.com/auth/colaboratory
      - https://www.googleapis.com/auth/userinfo.email
      - openid
    ```

## Implementation Details

-   **Code Injection**: Use a standard `run_code(session, code)` helper via
    `ColabRuntime`.
-   **History Management**: Use `HistoryLogger` class to append structured
    events to session-specific `.jsonl` files.
-   **Interactive Prompts**: Instrumented `stdin_hook` and `colab_request_hook`
    to record interactive user input and proprietary backend requests.

## Testing Strategy

TDD is mandatory for all automation features.

### 1. Mock Kernel Injection

-   **Test Case**: Verify `colab auth` correctly injects `from google.colab
    import auth; auth.authenticate_user()`.
-   **Test Case**: Verify `colab install` correctly injects `pip install` or `uv
    install` commands to the remote VM kernel.
-   **Test Case**: Verify `colab drivemount` correctly injects `drive.mount()`
    commands and registers the `colab_request_hook` to intercept credential
    propagation events.

### 2. History Capture

-   **Test Case**: Verify all code sent via `exec` is correctly appended to the
    JSON-L history file for that session.
-   **Test Case**: Verify `colab log` correctly generates an `.ipynb` from a
    populated history file.

### 3. `whoami` Identity Resolution

-   **Test Case**: Mock the credentials + `urllib.request.urlopen` to return a
    fake tokeninfo payload; verify the printed output contains the email, the
    active auth provider name, the scopes (one per line), and a human-readable
    expires-in (minutes, not raw seconds).
-   **Test Case**: When `urlopen` raises `HTTPError(400)` (revoked/expired
    token), `whoami` exits non-zero with a message identifying the failure
    rather than emitting an unhandled traceback.
-   **Test Case**: `colab --help` does NOT mention `whoami` (regression
    against accidental un-hiding) but `colab whoami --help` still shows the
    command's own help text.
-   **Test Case**: `creds.refresh()` is called before `creds.token` is read
    (regression against silently-`None` tokens for service-account /
    GCE-metadata creds).
