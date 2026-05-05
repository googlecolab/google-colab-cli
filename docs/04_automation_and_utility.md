---
lgtm: rtp
date: 2026-05-04
comments:
log:

2026-05-05: Fixed `colab url` to emit the correct connect URL format (b/509662345 follow-up). The first cut used the hash form `#datalabBackendUrl=<full URL>`, which Duckie had described — but a closer empirical search of google3 (`cs 'file:googledata/experiments/colab/features/development_flags.gcl "DBU"'`) revealed the actual form Colab uses in practice is the `dbu` query parameter, defined by the mendel param `datalab_backend_url` at `googledata/experiments/colab/features/development_flags.gcl:589` with `flag_name = 'dbu'`. The flag is consumed by `Traits.getDatalabBackendUrl` (`research/colab/frontend/common/model/traits.ts:359`) which resolves the value against `window.location.origin` and enforces same-origin. The new format is `https://<host>/notebooks/empty.ipynb?dbu=<urlencoded /tun/m/endpoint>` — `/notebooks/empty.ipynb` is a real route to a blank notebook (`research/colab/frontend/external/notebooks/empty.ipynb`) that gives the user a usable surface once the kernel attaches. The path value is URL-encoded with `safe=""` so any unusual characters in the endpoint round-trip cleanly. Tests in `tests/test_url.py` were updated (now 9 tests, +1 for special-character encoding) and assert via `urlparse`/`parse_qs` rather than fragile string contains. Verified live: `colab url -s url-test2` produced `https://colab.research.google.com/notebooks/empty.ipynb?dbu=%2Ftun%2Fm%2Fm-s-kkb-use4c0-rtd03u9nvduu`. Caveat: `dbu` is a development flag, so URL-overriding it is gated to sandbox users and Googlers; external users would need a different (hash-based) form, which we can add later if needed. Lessons re-encoded: AGENTS.md #14 (heed codesearch caveats — and verify Duckie claims with primary sources, since Duckie missed the gcl indirection).

2026-05-05: Added `colab url [-s SESSION] [--host HOST] [--open]` (b/509662345) — prints a browser URL that, when opened, makes the Colab frontend connect to an existing colab-cli session instead of provisioning a new VM. (Initial implementation used the `#datalabBackendUrl=` hash form; superseded same-day by the `?dbu=...` query form per the follow-up entry above.) Off-by-default `--open` mirrors `colab pay`'s `webbrowser.open()` behaviour but only when explicitly requested, so the command is pipeable by default (`colab url -s s1 | xclip`). Implementation lives in `commands/utility.py:url`; `url` is also added to `_AUTO_UPDATE_SUPPRESSED` in `cli.py` so the daily upgrade banner can't corrupt the printed URL when stdout is a pipe.

2026-05-04: Sorted the subcommand listing in `colab --help` and `colab help` alphabetically. Subcommands are still registered in functional groups (session, execution, files, automation, utility) inside `cli.py`, but the rendered help output is now deterministic. Implemented via a tiny `AlphabeticalGroup(TyperGroup)` subclass that overrides `list_commands()` to wrap the parent's result with `sorted(...)`, then passed as `cls=AlphabeticalGroup` to `typer.Typer(...)`. Two regression tests in `tests/test_cli.py` parse the rich-rendered command box and assert `names == sorted(names)` for both invocation paths.

2026-05-04: Expanded the auto-update banner suppression list in `cli.py`: in addition to `update` (which runs its own check + announce), the daily fetch and cached banner are now also suppressed for `version`, `log`, `pay`, and `help`. Rationale: these are short-lived informational subcommands whose output users routinely pipe / scrape (e.g. `colab version` in shell scripts, `colab log -o file.md` for export); a stochastic upgrade banner injected once a day would corrupt those pipelines and add noise to the help output. The suppression list is encoded as a small set literal `_AUTO_UPDATE_SUPPRESSED` in the global Typer callback. Existing tests that used `colab version` purely as a "noisy" command to trigger the background check were migrated to `colab sessions`.

2026-05-04: Cached the highest-known release version in a new `latest_version` Settings field, written by both `colab update` and the daily update-check fetch (whichever observed a newer version across `update_url` and `update_file_path`; never downgraded, and preserved across failed checks). The CLI's global callback now consults this cache on every invocation: if `latest_version` is strictly newer than the installed CLI, the upgrade banner is printed even on days where the network throttle would otherwise skip the check. The cached banner uses the generic `Run 'colab update' to update.` hint (since the cache doesn't track which source supplied the version) so the user can re-run `colab update` to get the source-specific install command. Renamed the master toggle `enable_auto_update` → `enable_update_check` (the toggle only governs *checking* for updates; the actual install only happens via `colab update --install`); legacy on-disk values under the old name are silently migrated by `SettingsStore.load`. Setting `enable_update_check=False` suppresses both the fetch and the cached banner.

2026-05-04: Extracted the auto-update subsystem into a dedicated module `colab_cli/auto_update.py`. It owns version detection (`get_app_version`), source fetching (`_fetch_pypi`, `_fetch_local`), version comparison (`_is_newer`, `_max_version`), the upgrade banner (`announce_upgrade`), the orchestration entry point (`check_for_updates`), the cached-banner helper (`maybe_show_cached_banner`), the throttle predicate (`_is_throttled`), the global callback hook (`run_background_check`), and the self-installer (`self_install`). `commands/utility.py` keeps the user-facing `update_command` and `version_command` and now delegates all heavy lifting to the new module; `cli.py` calls `auto_update.run_background_check()` instead of lazy-importing private helpers. Also added a `show_disable_hint` parameter to `announce_upgrade`: it is `True` for unsolicited paths (daily background fetch, cached banner) and `False` for explicit `colab update` invocations, so users who asked for the check don't get a "how to silence" footer they didn't need.

2026-03-19: Implemented `colab drivemount` utilizing a custom websocket hook in `ColabRuntime`. We successfully intercept the proprietary `colab_request` for the `dfs_ephemeral` auth type over the `iopub` channel, execute the necessary `/tun/m/credentials-propagation/` backend API requests (prompting the user interactively with the OAuth URL if consent is required), and then dispatch a specially crafted `input_reply` message back to the kernel via the `stdin` channel to seamlessly unblock the `drive.mount()` execution.

2026-03-19: Implemented `colab auth` and `colab install` utilizing the `ColabRuntime` injection layer. Modified `ColabRuntime.execute_code` to accept `allow_stdin` and `stdin_hook` parameters. Discovered that Google Colab's default web ephemeral auth expects a proprietary side-channel authentication mechanism, so we bypass it by defining `USE_AUTH_EPHEM='0'` which seamlessly falls back to the interactive `gcloud` verification flow natively supported by our `jupyter-kernel-client` implementation.

2026-03-23: Implemented `colab log` and `colab pay`. Comprehensive event history recording is now active across all major commands (new, stop, exec, repl, file ops, automation). Recorded events include execution code/outputs, stdin prompts/replies, and proprietary `colab_request` interceptions. `colab log` allows listing and viewing a summary timeline of these events. `colab pay` opens the signup page via the system browser.

2026-04-10: Implemented streaming output for `colab exec` and `colab repl`. Updated `ColabRuntime` to support `output_hook` via `execute_interactive` for real-time output delivery.

2026-04-10: Unified internal (LOAS2/Stubby) and public (OAuth2) authentication logic into a single `get_credentials` entry point. Added global `--auth-loas2` / `--auth-oauth2` flag to switch between authentication strategies.

2026-04-10: Hid the `colab auth` subcommand from help output to reduce user confusion, as it's an advanced command that is rarely needed manually.

2026-04-13: Implemented `colab version` command. It dynamically retrieves the version from installed package metadata or falls back to the git commit hash in development environments. Enabled dynamic versioning in `pyproject.toml` using `hatch-vcs`.

2026-04-16: Implemented auto-update feature. The CLI now checks for updates once a day by fetching a configurable URL (defaulting to GitHub). A new `colab update` command allows users to manually trigger a version check. Persistent state and settings are stored in `~/.config/colab-cli/settings.json`.

2026-04-24: Replaced the boolean `--auth-loas2/--auth-oauth2` flag with a single tri-state `--auth=loas2|oauth2|adc` enum option. Added a third authentication strategy backed by `google.auth.default()` (Application Default Credentials). `get_credentials` now dispatches on a new `AuthProvider` enum, and `State.auth_loas2` was renamed to `State.auth_provider`. Also fixed `drivemount` to honor the user's chosen provider (it previously always defaulted to LOAS2).

2026-04-28: Added a second update source: a local JSON file at `update_file_path` (default: `/google/src/files/head/depot/google3/experimental/colab/colab-cli/releases/version.json`). The file is parsed with the same PyPI-style `info.version` schema as `update_url`. The "up to date" output now includes the most recent version found across both sources, e.g. `Colab CLI is up to date (version: 1.0.0, latest: 1.0.0).` A missing file is silently ignored (the default path is internal-only); a malformed file emits a warning.

2026-04-28: Separated the two update paths so each owns its own install hint. When the local file reports a newer version it takes precedence: the install URL is read from `releases[<version>].url` in the same file and the hint becomes `Run 'uv tool install <URL>' to update.` (falling back to `uv tool install colab` when no release URL is recorded). Otherwise the PyPI source is used and the hint remains `Run 'pip install --upgrade colab' to update.`

2026-04-29: Added an opt-in `--install` flag to `colab update` that performs the upgrade in-place by shelling out to `uv tool install <URL>`. Scope is intentionally narrow: only the local-file source is auto-installed (PyPI upgrades still require a manual `pip install --upgrade`), the URL must be recorded in `releases[<version>].url`, and the flag is Linux-only — invoking it on macOS or Windows exits non-zero with an explanatory message. `check_for_updates` was extended to return the local release URL so the command layer can decide whether to install. Default is `False` to preserve the prior "check-only" behavior.

2026-04-30: Fixed scope handling on each auth provider so the keep-alive RPC at `colab.pa.googleapis.com` can succeed (the Boq RuntimeService requires the `https://www.googleapis.com/auth/colaboratory` OAuth scope and rejects unscoped tokens with 403 `SCOPE_NOT_PERMITTED`).
- **OAuth2**: `PUBLIC_SCOPES` already contained `colaboratory`, so the InstalledAppFlow consent screen requested it correctly. Cached tokens minted before this fix should be deleted to trigger a fresh consent flow.
- **ADC**: `_get_adc_credentials` now passes `scopes=PUBLIC_SCOPES` to `google.auth.default()` AND re-applies via `creds.with_scopes()` for credential subclasses that support it (service accounts, GCE/GKE, impersonated). User credentials minted by `gcloud auth application-default login` ignore both mechanisms and need re-issuing with `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory`. Both scopes are mandatory: `userinfo.email` for the TFE backend at `colab.research.google.com` (assign/unassign/sessions return 401 without it), and `colaboratory` for the Boq RuntimeService at `colab.pa.googleapis.com` (keep-alive returns 403 without it).
- **LOAS2**: We attempted to add `colaboratory` to the stubby `CorpLogin.Exchange` request (proto-correct: `oauth2_attributes.scope` is a repeated string). The corp LOAS-to-OAuth exchange policy at `security/corplogin/exchange/policy/policy.ncl` does NOT allowlist `colaboratory` for general Googler use; the request is rejected with "Your request did not match any of the authorized CXF policy rules" (verified empirically and confirmed by Duckie). The change was reverted: LOAS2 again requests only `userinfo.email`. Because keep-alive is now known to be impossible under LOAS2 without a policy exception, `commands/session.py:new` skips both the keep-alive pre-flight and the daemon spawn under `--auth=loas2`, printing a one-line warning that the VM will idle-timeout in ~90min. Long-term fix is a policy exception via go/enterprise-identity-intake.
- **Pre-flight**: For non-LOAS2 providers, `colab new` now invokes `keep_alive_assignment` once synchronously after `assign` succeeds. On 403 `SCOPE_NOT_PERMITTED`, it unassigns the just-created VM (so we don't leak a billable assignment) and prints a per-provider remediation message before exiting non-zero.

---

# Design: Automation and Utility (`auth`, `install`, `log`, `pay`, `version`, `update`)

## Overview

These subcommands are implemented by executing Python code on the Colab VM,
managing local state, or inspecting the environment.

## Authentication Strategies (CLI Backend)

The CLI supports three authentication strategies for talking to the Colab
backend, selected via the global `--auth=<provider>` flag:

1.  **`loas2`** (default): Uses `stubby` and `gcert` to exchange corporate
    LOAS2 credentials for a short-lived OAuth2 access token. Internal-only.
2.  **`oauth2`**: Standard public InstalledAppFlow via `google-auth-oauthlib`.
    Opens a browser for consent, caches the refresh token at
    `~/.config/colab-cli/token.json`. Requires a client OAuth config (either
    `~/.colab-cli-oauth-config.json`, the path passed via
    `-c/--client-oauth-config`, or the bundled `oauth_config.json` resource).
3.  **`adc`**: Application Default Credentials via `google.auth.default()`.
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

-   `colab.research.google.com` (TFE — assignment / unassignment / contents
    API): the `userinfo.email` scope is sufficient.
-   `colab.pa.googleapis.com` (Boq `RuntimeService`, used by
    `KeepAliveAssignment`): **requires** the
    `https://www.googleapis.com/auth/colaboratory` scope. Without it, every
    request returns HTTP 403 with body `[7,"Request had insufficient
    authentication scopes.",...]` and a `DebugInfo` mentioning
    `SCOPE_NOT_PERMITTED`. (The Boq frontend additionally requires
    `X-Goog-Api-Client` to contain `grpc-web` — see
    `01_session_management.md` §5.)

How each provider supplies the scope:

-   **`loas2`**: The corp LOAS-to-OAuth exchange policy
    (`security/corplogin/exchange/policy/policy.ncl`) does NOT allowlist
    `colaboratory` for general Googler use. Requesting it from
    `CorpLogin.Exchange` returns "Your request did not match any of the
    authorized CXF policy rules" — the request fails outright and we get no
    token at all. As a consequence, **keep-alive is intentionally disabled
    under `--auth=loas2`**: `commands/session.py:new` skips both the
    pre-flight ping and the daemon spawn, printing a one-line warning that
    the VM will idle-timeout in ~90 minutes. The long-term fix is to file
    a policy exception via go/enterprise-identity-intake; in the interim,
    use `--auth=oauth2` for any session that needs to live beyond the idle
    timeout.
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
        --scopes=https://www.googleapis.com/auth/userinfo.email,\
    https://www.googleapis.com/auth/colaboratory
    ```

    Both scopes are required: `userinfo.email` for the TFE backend at
    `colab.research.google.com` (otherwise assign/unassign/sessions return
    HTTP 401), and `colaboratory` for the Boq RuntimeService at
    `colab.pa.googleapis.com` (otherwise keep-alive returns HTTP 403).

For OAuth2 and ADC, `colab new` performs a one-shot keep-alive pre-flight
after `assign` succeeds so missing-scope failures surface immediately
(with per-provider remediation guidance) rather than silently after ~1
minute via the daemon. Under LOAS2 the pre-flight is skipped (we already
know it would fail).

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
    during the execution of any command. Independently, the cached
    `latest_version` (see below) is consulted on **every** invocation so the
    upgrade banner remains visible between fetches without requiring a network
    round-trip.
-   **Suppressed subcommands**: To keep machine-parseable output clean, the
    daily fetch and the cached banner are suppressed for `update` (which
    runs its own check), `version`, `log`, `pay`, and `help`. The list lives
    as `_AUTO_UPDATE_SUPPRESSED` in the global Typer callback in `cli.py`.
-   **Manual-check**: `colab update` forces a check and prints the status.
-   **Implementation**:
    -   Fetches a JSON file from a configurable `update_url` (default:
        `https://raw.githubusercontent.com/googlecolab/colab-cli/main/version.json`).
    -   Additionally reads a JSON file from a configurable `update_file_path`
        (default:
        `/google/src/files/head/depot/google3/experimental/colab/colab-cli/releases/version.json`).
        Both sources use the same PyPI-style `info.version` schema. The two
        sources are evaluated independently:
        -   If the local file reports a version newer than the installed one
            it takes precedence. The install URL is read from
            `releases[<version>].url` in the same file and the printed hint
            becomes `Run 'uv tool install <URL>' to update.` (falling back to
            `uv tool install colab` when the URL is missing).
        -   Otherwise, if the PyPI source reports a newer version the hint is
            the standard `Run 'pip install --upgrade colab' to update.`
        A missing local file is silently ignored (the default path is only
        resolvable inside Google's Piper workspace); other read/parse failures
        emit a non-fatal warning.
    -   Compares the resulting `version` with the current CLI version using
        PEP 440 / semantic versioning, falling back to string equality when a
        version is unparseable.
    -   Persists the following fields in `~/.config/colab-cli/settings.json`:
        -   `update_url`, `update_file_path`: source configuration.
        -   `last_check`: timestamp of the last fetch (drives the daily
            throttle).
        -   `enable_update_check`: master switch for both the daily fetch and
            the cached banner.
        -   `latest_version`: highest version observed across all sources
            during the most recent successful check. Updated whenever a
            strictly-newer version is observed (never downgraded), and
            preserved verbatim across failed checks so transient network
            issues do not erase the cache.
-   **Notification**: If a new version is found, a non-intrusive message is
    printed to the console. The cached banner shown between fetches uses the
    generic `Run 'colab update' to update.` hint (because the cache does not
    record which source supplied the version); running `colab update` then
    re-fetches and prints the source-specific install command.
-   **Self-install (`--install`)**: An opt-in `--install` flag (default
    `False`) makes `colab update` shell out to `uv tool install <URL>` after
    the version check, where `<URL>` is the local-file release URL described
    above. The flag is intentionally scoped to the local-file source — PyPI
    upgrades remain manual — and is **Linux only**; on other platforms it
    exits with a non-zero status and an error message. When no newer release
    with a URL is available the flag is a silent no-op so it is safe to wire
    into automation.

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
