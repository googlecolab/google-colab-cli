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
# Integration Test: Keep-Alive Daemon Soak (OAuth Scope Regression Guard)
#
# Background:
#   On 2026-04-30, a regression was discovered where `colab new` would succeed
#   but the keep-alive daemon would silently die ~1 minute later, causing the
#   VM to be idle-pruned shortly after. Two underlying causes:
#     (a) The Boq RuntimeService at colab.pa.googleapis.com requires
#         `X-Goog-Api-Client` to contain `grpc-web`. Missing this returns 400.
#     (b) The same service requires the
#         `https://www.googleapis.com/auth/colaboratory` OAuth scope.
#         Missing this returns 403 SCOPE_NOT_PERMITTED.
#   Both unit-test layers passed because they mock the network. The bug only
#   surfaces against the live backend.
#
# What this test does:
#   1. Spawns a real Colab session via `colab new`.
#   2. Waits 90 seconds — long enough for the daemon to hit at least one
#      ping iteration *after* the pre-flight (the loop sleeps 60s between
#      pings).
#   3. Reads the structured history via `colab log` and asserts NO
#      `KEEP: error` events were recorded. Any error event means a daemon
#      ping was rejected by the server, which is the regression.
#   4. Verifies the daemon process is still alive.
#   5. Cleans up via `colab stop`.
#
# Cost: ~95 seconds of real wall-clock time + one short-lived Colab CPU
# assignment.

set -e

# Use a uniquely-named session per run so we don't trip on stale history
# from previous runs (history files are keyed by session name and live at
# ~/.config/colab-cli/history/<name>.jsonl).
SESSION_NAME="repro-keep-alive-scope-$(date +%s)"

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"

# This test soaks the keep-alive daemon for 90s, so it is meaningful only on
# auth providers that actually spawn a daemon. LOAS2 disables keep-alive by
# design (corp policy doesn't allowlist the colaboratory scope — see
# auth.py), so we skip the soak entirely under LOAS2 and require either
# OAuth2 or properly-scoped ADC.
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
    else
        echo "Skipping: ADC token lacks required scopes (need both"
        echo "          userinfo.email and colaboratory). Re-issue with:"
        echo "          gcloud auth application-default login \\"
        echo "              --scopes=https://www.googleapis.com/auth/userinfo.email,\\"
        echo "                      https://www.googleapis.com/auth/colaboratory"
        exit 0  # environment-not-applicable
    fi
else
    echo "Skipping: this test requires --auth=oauth2 or properly-scoped ADC"
    echo "          because keep-alive is disabled under --auth=loas2 by design."
    echo "          Bootstrap options:"
    echo "          - OAuth2: 'uv run colab --auth=oauth2 sessions' (browser consent)"
    echo "          - ADC:    gcloud auth application-default login \\"
    echo "                        --scopes=https://www.googleapis.com/auth/userinfo.email,\\"
    echo "                                https://www.googleapis.com/auth/colaboratory"
    exit 0  # environment-not-applicable
fi

cleanup() {
    echo "[*] Cleaning up..."
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
    # Best-effort: scrub the history file so the test is idempotent.
    rm -f "$HOME/.config/colab-cli/history/${SESSION_NAME}.jsonl"
}
trap cleanup EXIT

echo "[*] Creating session '$SESSION_NAME' (REAL API CALL) using $AUTH_FLAGS..."
# Note: `colab new` now performs a synchronous keep-alive pre-flight. If the
# OAuth scope is missing, this command itself will fail fast with an
# actionable remediation message — so step (1) of the regression already
# fires here.
if ! uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"; then
    echo "[FAILURE] 'colab new' failed. If this is a SCOPE_NOT_PERMITTED error,"
    echo "          the colaboratory scope is missing from your auth provider."
    echo "          For ADC: gcloud auth application-default login \\"
    echo "                       --scopes=https://www.googleapis.com/auth/cloud-platform,\\"
    echo "                                https://www.googleapis.com/auth/colaboratory"
    exit 1
fi

# Sanity-check the session was persisted with a daemon PID.
PID=$(grep -A 15 "$SESSION_NAME" "$SESSION_FILE" | grep "keep_alive_pid" | awk '{print $2}' | tr -d ',')
if [ -z "$PID" ] || [ "$PID" == "null" ]; then
    echo "[FAILURE] No keep_alive_pid recorded for session."
    cat "$SESSION_FILE"
    exit 1
fi
echo "[*] Keep-alive daemon PID: $PID"

# Soak: wait long enough for at least one daemon-driven ping (loop sleeps
# 60s) to land *after* the pre-flight that `colab new` did. 90s gives us a
# comfortable margin.
echo "[*] Soaking for 90s to let the daemon perform at least one ping..."
sleep 90

# The daemon must still be alive.
if ! ps -p $PID > /dev/null; then
    echo "[FAILURE] Keep-alive daemon (pid=$PID) died during soak."
    echo "          History dump:"
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" log -s "$SESSION_NAME" || true
    exit 1
fi
echo "[*] Daemon still alive after 90s."

# The structured history must NOT contain any keep_alive_error events. Any
# error here means a server-side rejection (auth, headers, payload) — the
# exact class of bug this test guards against.
LOG_OUTPUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" log -s "$SESSION_NAME")
echo "----- colab log output -----"
echo "$LOG_OUTPUT"
echo "----------------------------"

if echo "$LOG_OUTPUT" | grep -q "KEEP: error"; then
    echo "[FAILURE] keep_alive_error events recorded during soak."
    echo "          This indicates the daemon's pings are being rejected."
    echo "          Common causes:"
    echo "            - Missing 'colaboratory' OAuth scope (403 SCOPE_NOT_PERMITTED)"
    echo "            - Missing X-Goog-Api-Client: grpc-web header (400 Invalid GRPC-Web)"
    exit 1
fi

# Positive assertion: we expect at least one KEEP: started event.
if ! echo "$LOG_OUTPUT" | grep -q "KEEP: started"; then
    echo "[FAILURE] No KEEP: started event recorded — daemon never ran?"
    exit 1
fi

echo "[SUCCESS] Keep-alive daemon survived 90s soak with zero error events."
