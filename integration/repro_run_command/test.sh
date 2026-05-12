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

# Integration Test: `colab run <script.py> [args...]`
#
# Verifies the shebang-friendly one-shot execution flow:
#   1. `colab run` allocates a CPU VM, runs the script, releases the VM.
#   2. `sys.argv` and `__name__ == "__main__"` are honored.
#   3. After the run finishes, no orphan VMs remain.
#   4. `colab run --keep` leaves the session alive; `colab stop` clears it.

# Don't `set -e` so we can capture failures and clean up explicitly.

# ---------- Auth detection (mirrors integration/repro_keep_alive/test.sh) ----
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
    else
        echo "Error: ADC token lacks the required scopes (colaboratory + userinfo.email)."
        echo "Re-issue ADC creds with all required scopes:"
        echo "  gcloud auth application-default login \\"
        echo "      --scopes=openid,\\"
        echo "              https://www.googleapis.com/auth/cloud-platform,\\"
        echo "              https://www.googleapis.com/auth/userinfo.email,\\"
        echo "              https://www.googleapis.com/auth/colaboratory"
        exit 1
    fi
else
    echo "Error: No usable auth provider found."
    exit 1
fi
echo "[*] Using $AUTH_FLAGS"

# ---------- Isolated session state -------------------------------------------
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SCRIPT_PATH="$TMP_DIR/script.py"
KEEP_SESSION_NAME="repro-run-keep-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$KEEP_SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cat > "$SCRIPT_PATH" <<'PYEOF'
import sys
print(f"argv={sys.argv}")
print(f"is_main={__name__ == '__main__'}")
PYEOF
SCRIPT_BASENAME=$(basename "$SCRIPT_PATH")

# ---------- Phase 1: basic run + auto-cleanup --------------------------------
echo "[*] Phase 1: colab run <script.py> hello world"
OUTPUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" run "$SCRIPT_PATH" hello world 2>&1)
RC=$?
echo "$OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] colab run exited $RC"
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "argv=\['$SCRIPT_BASENAME', 'hello', 'world'\]"; then
    echo "[FAILURE] argv was not propagated as expected."
    echo "  Wanted substring: argv=['$SCRIPT_BASENAME', 'hello', 'world']"
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "is_main=True"; then
    echo "[FAILURE] __name__ was not set to '__main__'."
    exit 1
fi

# Verify cleanup actually happened — no orphan assignments remain.
SESSIONS_OUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" sessions 2>&1)
echo "$SESSIONS_OUT"
if ! echo "$SESSIONS_OUT" | grep -q "No active sessions found on server."; then
    echo "[FAILURE] After auto-cleanup, server still reports active sessions."
    echo "          (Possible orphan VM — investigate.)"
    exit 1
fi
echo "[SUCCESS] Phase 1 passed: argv passthrough, __main__, and auto-cleanup."

# ---------- Phase 2: --keep leaves the session alive -------------------------
echo ""
echo "[*] Phase 2: colab run --keep -s $KEEP_SESSION_NAME <script.py>"
OUTPUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" run --keep -s "$KEEP_SESSION_NAME" "$SCRIPT_PATH" keep_arg 2>&1)
RC=$?
echo "$OUTPUT"

if [ $RC -ne 0 ]; then
    echo "[FAILURE] colab run --keep exited $RC"
    exit 1
fi
if ! echo "$OUTPUT" | grep -q "argv=\['$SCRIPT_BASENAME', 'keep_arg'\]"; then
    echo "[FAILURE] --keep run did not produce the expected argv output."
    exit 1
fi

SESSIONS_OUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" sessions 2>&1)
echo "$SESSIONS_OUT"
if ! echo "$SESSIONS_OUT" | grep -q "\[$KEEP_SESSION_NAME\]"; then
    echo "[FAILURE] --keep session $KEEP_SESSION_NAME not found in colab sessions."
    exit 1
fi

uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$KEEP_SESSION_NAME"
SESSIONS_OUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" sessions 2>&1)
if ! echo "$SESSIONS_OUT" | grep -q "No active sessions found on server."; then
    echo "[FAILURE] After manual stop of $KEEP_SESSION_NAME, sessions remain."
    exit 1
fi

echo "[SUCCESS] Phase 2 passed: --keep persists the session, manual stop clears it."
echo "[SUCCESS] All phases passed."
exit 0
