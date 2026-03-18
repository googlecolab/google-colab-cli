---
lgtm: rtp
date: 2026-03-19
comments:
log:

2026-03-19: Implemented `colab drivemount` utilizing a custom websocket hook in `ColabRuntime`. We successfully intercept the proprietary `colab_request` for the `dfs_ephemeral` auth type over the `iopub` channel, execute the necessary `/tun/m/credentials-propagation/` backend API requests (prompting the user interactively with the OAuth URL if consent is required), and then dispatch a specially crafted `input_reply` message back to the kernel via the `stdin` channel to seamlessly unblock the `drive.mount()` execution.

2026-03-19: Implemented `colab auth` and `colab install` utilizing the `ColabRuntime` injection layer. Modified `ColabRuntime.execute_code` to accept `allow_stdin` and `stdin_hook` parameters. Discovered that Google Colab's default web ephemeral auth expects a proprietary side-channel authentication mechanism, so we bypass it by defining `USE_AUTH_EPHEM='0'` which seamlessly falls back to the interactive `gcloud` verification flow natively supported by our `jupyter-kernel-client` implementation.

2026-03-23: Implemented `colab log` and `colab pay`. Comprehensive event history recording is now active across all major commands (new, stop, exec, repl, file ops, automation). Recorded events include execution code/outputs, stdin prompts/replies, and proprietary `colab_request` interceptions. `colab log` allows listing and viewing a summary timeline of these events. `colab pay` opens the signup page via the system browser.

2026-04-10: Implemented streaming output for `colab exec` and `colab repl`. Updated `ColabRuntime` to support `output_hook` via `execute_interactive` for real-time output delivery.

2026-04-10: Unified internal (LOAS2/Stubby) and public (OAuth2) authentication logic into a single `get_credentials` entry point. Added global `--auth-loas2` / `--auth-oauth2` flag to switch between authentication strategies.

2026-04-10: Hid the `colab auth` subcommand from help output to reduce user confusion, as it's an advanced command that is rarely needed manually.

2026-04-13: Implemented `colab version` command. It dynamically retrieves the version from installed package metadata or falls back to the git commit hash in development environments. Enabled dynamic versioning in `pyproject.toml` using `hatch-vcs`.

2026-04-16: Implemented auto-update feature. The CLI now checks for updates once a day by fetching a configurable URL (defaulting to GitHub). A new `colab update` command allows users to manually trigger a version check. Persistent state and settings are stored in `~/.config/colab-cli/settings.json`.

---

# Design: Automation and Utility (`auth`, `install`, `log`, `pay`, `version`, `update`)

## Overview

These subcommands are implemented by executing Python code on the Colab VM,
managing local state, or inspecting the environment.

## Authentication Strategies (CLI Backend)

The CLI supports two authentication methods for interacting with the Colab
backend: 1. **Internal (LOAS2)**: Uses `stubby` and `gcert` to exchange
corporate credentials for an OAuth2 token. This is the default mode, explicitly
enabled with `--auth-loas2`. 2. **Public (OAuth2)**: Uses standard
`google-auth-oauthlib` for external users. This is activated by the
`--auth-oauth2` flag.

These flags are mutually exclusive toggles.

The `get_credentials` entry point in `auth.py` abstracts these strategies,
allowing the core `Client` to remain authentication-agnostic.

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
    during the execution of any command.
-   **Manual-check**: `colab update` forces a check and prints the status.
-   **Implementation**:
    -   Fetches a JSON file from a configurable `update_url` (default:
        `https://raw.githubusercontent.com/googlecolab/colab-cli/main/version.json`).
    -   Compares the `version` field in the JSON with the current CLI version using semantic versioning.
    -   Stores the last check timestamp and update URL in
        `~/.config/colab-cli/settings.json`.
-   **Notification**: If a new version is found, a non-intrusive message is
    printed to the console.

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
