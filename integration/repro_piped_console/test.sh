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
# Integration Test: Piped console exits cleanly on EOF
# Verifies that `echo cmd | colab console -s s` runs the command on the remote
# /colab/tty endpoint and then exits within a few seconds.
#
# Regression: prior to 2026-05-07 this hung indefinitely because the EOF
# handler sent \x04 (Ctrl-D), which the remote tmux-wrapped bash treated as a
# literal character rather than a session terminator. The fix sends "exit\n"
# and closes the websocket from the client side after a short grace period.

set -e

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION_NAME="test-piped-console"

# Auth selection (same priority order as repro_keep_alive).
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
    else
        echo "Error: ADC token lacks the userinfo.email scope."
        exit 1
    fi
else
    echo "Error: No usable auth provider found." >&2
    exit 1
fi

cleanup_session() {
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
}
trap "cleanup_session; rm -rf $TMP_DIR" EXIT

echo "[*] Creating session for piped-console test using $AUTH_FLAGS..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

# Marker string we'll grep for in the captured output. Made unique enough that
# accidental matches in tmux status lines or shell prompts are impossible.
MARKER="PIPED-CONSOLE-OK-$(date +%s)-$$"

OUTPUT_FILE="$TMP_DIR/console-output.txt"

echo "[*] Running: echo 'echo $MARKER' | colab console (must exit within 30s)..."
START=$(date +%s)
# 30s upper bound guards against the prior hang regression. The fix typically
# completes in ~1-2s; anything beyond that means the EOF/close path broke.
if ! timeout 30 bash -c "echo 'echo $MARKER' | uv run colab $AUTH_FLAGS --config '$SESSION_FILE' console -s '$SESSION_NAME'" > "$OUTPUT_FILE" 2>&1; then
    EXIT=$?
    if [ $EXIT -eq 124 ]; then
        echo "[FAILURE] Piped console hung past the 30s timeout (regression — was the EOF handler reverted?)."
    else
        echo "[FAILURE] Piped console exited non-zero: $EXIT"
    fi
    cat "$OUTPUT_FILE"
    exit 1
fi
ELAPSED=$(($(date +%s) - START))
echo "[*] Piped console returned in ${ELAPSED}s."

if ! grep -q "$MARKER" "$OUTPUT_FILE"; then
    echo "[FAILURE] Marker '$MARKER' not found in console output (the command did not actually execute on the VM)."
    cat "$OUTPUT_FILE"
    exit 1
fi
echo "[*] Confirmed: the piped command actually ran on the remote VM."

# Sanity: the doc claims ~1.2s round-trip. Anything > 10s is suspicious even
# if it eventually exited (probably means the grace window was bumped wildly).
if [ "$ELAPSED" -gt 10 ]; then
    echo "[WARNING] Piped console took ${ELAPSED}s — slow, but not a hang."
fi

echo "[SUCCESS] Piped console integration test passed (elapsed=${ELAPSED}s)."
