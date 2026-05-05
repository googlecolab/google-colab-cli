# Real-World Integration Scenarios

This directory contains end-to-end integration tests and reproduction scripts for user-reported issues. Unlike the unit tests in `tests/`, these are intended to be run against a **live Colab environment**.

## **Prerequisites**
- A valid Google account with Colab access.
- `uv` installed locally.
- Authenticated state (run `colab sessions` to verify you can talk to the backend).

## **Scenarios**

### **1. Plot Redirection (`repro_plot_redirection/`)**
Tests the ability to execute a matplotlib script and redirect the intercepted plot to a specific local file.
- **Source**: User feedback regarding "Implicit Plot Handling".
- **Verified in**: v0.1.2

### **2. Keep-Alive Daemon Lifecycle (`repro_keep_alive/`)**
Fast smoke test (~10s). Verifies that `colab new` spawns a detached keep-alive daemon, persists its PID in the session state, that no `keep_alive_error` events fired during the synchronous pre-flight ping, and that `colab stop` reaps the daemon cleanly.
- **Source**: Original keep-alive feature work.
- **Run**: `uv run bash integration/repro_keep_alive/test.sh`

### **3. Keep-Alive OAuth Scope Soak (`repro_keep_alive_scope/`)**
Slow soak test (~95s). Spawns a real session, waits long enough for the daemon's 60-second ping loop to run at least one iteration *after* the pre-flight, then asserts no `keep_alive_error` events were recorded. Guards against the 2026-04-30 regression class where the keep-alive daemon was being silently killed by the Boq RuntimeService rejecting requests for missing `X-Goog-Api-Client: grpc-web` header (HTTP 400) or missing `https://www.googleapis.com/auth/colaboratory` OAuth scope (HTTP 403 `SCOPE_NOT_PERMITTED`).
- **Source**: Live debugging session, 2026-04-30.
- **Run**: `uv run bash integration/repro_keep_alive_scope/test.sh`

### **4. Variable Persistence (`repro_variable_persistence/`)**
Verifies that variables persist across `colab exec` invocations within the same session.

## **How to add a new scenario**
1. Create a sub-directory `repro_<issue_description>`.
2. Include a script (Python or Shell) that demonstrates the issue or verifies the fix.
3. Add a brief entry to this README, including a note about whether it's a fast smoke test or a slow soak test.
