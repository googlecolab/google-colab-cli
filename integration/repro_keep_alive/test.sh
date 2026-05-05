#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Integration Test: Keep-Alive Daemon Lifecycle
# Verifies that `colab new` spawns a detached keep-alive daemon, persists its
# PID in the session state, and that `colab stop` reaps it cleanly.
#
# This test is a fast smoke test (~10s). For a soak test that verifies the
# daemon's pings actually succeed against the live backend, see
# integration/repro_keep_alive_scope/.

set -e

# Setup a clean session file for testing
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
trap "rm -rf $TMP_DIR" EXIT

# Keep-alive is intentionally disabled under --auth=loas2 (the corp
# LOAS-to-OAuth exchange policy doesn't allowlist the `colaboratory` scope —
# see auth.py and go/enterprise-identity-intake). To exercise the actual
# daemon we need OAuth2 or ADC with the colaboratory scope. Selection
# priority: OAuth2 > ADC (with scope) > LOAS2 (reduced assertions).
EXPECT_DAEMON=0
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
    EXPECT_DAEMON=1
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    # Check that ADC has both required scopes (userinfo.email + colaboratory).
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
        EXPECT_DAEMON=1
    elif [ -x "$(command -v gcertstatus)" ] && gcertstatus --check_remaining=1m --quiet; then
        AUTH_FLAGS="--auth=loas2"
        echo "Note: running with LOAS2 — keep-alive daemon is disabled by design;"
        echo "      this run will only verify session creation/teardown."
    else
        echo "Error: ADC token lacks required scopes and no LOAS2 fallback."
        echo "Re-issue ADC creds with both required scopes:"
        echo "  gcloud auth application-default login \\"
        echo "      --scopes=https://www.googleapis.com/auth/userinfo.email,\\"
        echo "              https://www.googleapis.com/auth/colaboratory"
        exit 1
    fi
elif [ -x "$(command -v gcertstatus)" ] && gcertstatus --check_remaining=1m --quiet; then
    AUTH_FLAGS="--auth=loas2"
    echo "Note: running with LOAS2 — keep-alive daemon is disabled by design;"
    echo "      this run will only verify session creation/teardown."
else
    echo "Error: No usable auth provider found."
    echo "Options:"
    echo "  - gcert (for --auth=loas2): run 'gcert'"
    echo "  - OAuth2: run 'uv run colab --auth=oauth2 sessions' to bootstrap"
    echo "  - ADC: gcloud auth application-default login \\"
    echo "             --scopes=https://www.googleapis.com/auth/userinfo.email,\\"
    echo "                     https://www.googleapis.com/auth/colaboratory"
    exit 1
fi

SESSION_NAME="test-live-keep-alive"

cleanup_session() {
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
}
trap "cleanup_session; rm -rf $TMP_DIR" EXIT

echo "[*] Creating new session (REAL API CALL) using $AUTH_FLAGS..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

# Verify session exists in state
if [ ! -f "$SESSION_FILE" ]; then
    echo "Error: Session file '$SESSION_FILE' not created."
    exit 1
fi

grep "$SESSION_NAME" "$SESSION_FILE"

# Extract PID (may be null if keep-alive was intentionally disabled).
PID=$(grep -A 15 "$SESSION_NAME" "$SESSION_FILE" | grep "keep_alive_pid" | awk '{print $2}' | tr -d ',')

if [ "$EXPECT_DAEMON" -eq 0 ]; then
    if [ -n "$PID" ] && [ "$PID" != "null" ]; then
        echo "[FAILURE] keep_alive_pid is set under LOAS2 — expected null."
        cat "$SESSION_FILE"
        exit 1
    fi
    echo "[*] Confirmed keep-alive daemon was correctly disabled under LOAS2."
else
    if [ -z "$PID" ] || [ "$PID" == "null" ]; then
        echo "[FAILURE] No keep_alive_pid found under $AUTH_FLAGS (daemon should have spawned)."
        cat "$SESSION_FILE"
        exit 1
    fi
    echo "[*] Keep-alive PID: $PID"

    if ps -p $PID > /dev/null; then
       echo "[*] Keep-alive process is running."
    else
       echo "[FAILURE] Keep-alive process NOT running."
       exit 1
    fi

    # Verify the process command line is actually colab keep-alive
    ps -fp $PID | grep "keep-alive"

    LOG_OUTPUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" log -s "$SESSION_NAME")
    echo "$LOG_OUTPUT"
    if ! echo "$LOG_OUTPUT" | grep -q "KEEP: started"; then
        echo "[FAILURE] keep_alive_started event missing from history."
        exit 1
    fi
    # The pre-flight in `colab new` calls keep_alive_assignment once
    # synchronously before returning. If that succeeded, the structured
    # history should NOT contain any KEEP: error events at this point.
    if echo "$LOG_OUTPUT" | grep -q "KEEP: error"; then
        echo "[FAILURE] keep_alive_error events present immediately after 'colab new'."
        echo "          The pre-flight keep-alive ping failed. Check the body= field."
        exit 1
    fi
fi

echo "[*] Stopping session (REAL API CALL)..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME"
sleep 1

if [ "$EXPECT_DAEMON" -eq 1 ]; then
    if ! ps -p $PID > /dev/null; then
       echo "[*] Keep-alive process terminated successfully."
    else
       echo "[FAILURE] Keep-alive process still running after stop!"
       kill $PID
       exit 1
    fi
fi

# Disable the cleanup trap; we already cleaned up.
trap "rm -rf $TMP_DIR" EXIT

echo "[SUCCESS] Live integration test passed!"
