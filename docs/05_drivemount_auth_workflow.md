# Colab CLI: Drive Mount and Authentication Implementation Log

**Date:** March 19, 2026
**Participants:** Developer (rtp), Gemini CLI Agent

## Objective
Implement headless/CLI credential propagation to support `colab drivemount` and fix issues with `colab auth`. The challenge was intercepting the proprietary `colab_request` / `colab_reply` messages sent over the Jupyter WebSocket to emulate the Google Colab browser-frontend JIT OAuth consent flow.

## 1. Investigation & Discovery
The Colab backend API strictly handles credentials via two paths:
- `auth_user_ephemeral`: Used by `auth.authenticate_user()`.
- `dfs_ephemeral`: Used by `drive.mount()`.

Initially, we suspected that `dfs_ephemeral` was failing in the CLI because we lacked the `render_data_token` embedded in the Colab HTML UI. However, after executing an isolated HTTP dry-run trace, we discovered:
**Discovery:** The backend `POST /tun/m/credentials-propagation/{endpoint}?dryrun=true` actually succeeds for `dfs_ephemeral` WITHOUT the `render_data_token` (it is only required for `dfs_persistent` ARI audit logs).

## 2. The Architectural Gap (Why the Kernel Hung)
We found that Colab's `drive.mount()` does not possess a traditional terminal-based `gcloud` fallback (unlike `auth.authenticate_user()` which can be triggered via `USE_AUTH_EPHEM='0'`). 
When `drive.mount()` executes, the Python kernel sends a custom `colab_request` containing `{"authType": "dfs_ephemeral"}` over the **iopub** websocket channel and waits indefinitely for a `colab_reply` message on the **stdin** channel. Because the CLI didn't intercept or reply to this request, the kernel locked up.

## 3. The Implementation Challenges
Attempting to intercept this message via the standard `jupyter-kernel-client` execute hooks (`output_hook`) failed.
**Problem 1:** `jupyter-kernel-client` aggressively filters out any messages on the `iopub` channel that do not possess a `parent_header` matching the current execution request's `msg_id`. `colab_request` messages do not have this `msg_id` in their `parent_header` (they only include the session ID), so the library dropped them.

**Solution 1:** We monkey-patched the underlying WebSocket `_on_message` callback inside our `ColabRuntime` initialization. This allowed us to inspect every raw, deserialized message arriving from the server *before* it was filtered or queued by `jupyter-kernel-client`. We exposed this as `colab_request_hook`.

**Problem 2:** Constructing the reply. The kernel expects the frontend to reply to the `colab_request` to unblock execution. Initially, sending a raw `colab_reply` message back to the `stdin` channel failed.
**Solution 2:** Referencing our HAR traces, we discovered that Google Colab wraps the reply. The message type must be `input_reply`, and the payload `value` must be the JSON object `{"type": "colab_reply", "colab_msg_id": 1}`. We updated the CLI to construct this nested structure and assigned the intercepted message's `header` as the reply's `parent_header`.

## 4. Fixing `colab auth`
While testing `colab auth` using the `gcloud` fallback (`USE_AUTH_EPHEM='0'`), the kernel hung after the user pasted the verification code.
**Problem:** Our custom `interactive_stdin_hook` was correctly calling `input()`, but it only *returned* the typed string; it never sent it back to the kernel.
**Solution:** `jupyter-kernel-client` provides a robust default `_stdin_hook_default` which already handles `getpass`/`input` AND correctly sends the `input_reply` back over the `stdin` channel. By removing our custom hook and passing `allow_stdin=True` to `execute()`, we allowed the library to handle the TTY input natively, completely resolving the hang.

## 5. Result
The CLI can now autonomously intercept, authorize, and resume Colab Drive mounting (`colab drivemount`) and standard GCP authentication (`colab auth`) in a purely headless environment, accurately proxying the user's local OAuth consent without requiring an actual browser running against the Colab frontend.
