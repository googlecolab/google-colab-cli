#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# CPU-only end-to-end regression for issue #106. Every command starts with a
# deliberately invalid saved runtime-proxy token; session resolution must adopt
# the fresh token returned by /tun/m/assignments before touching the VM.

set -euo pipefail

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SERVER_SESSION_FILE="$TMP_DIR/server-snapshot.json"
SESSION_NAME="token-refresh-$PPID-$$"
TEST_ENDPOINT=""

if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_PROVIDER="oauth2"
elif command -v gcloud >/dev/null && gcloud auth application-default print-access-token >/dev/null 2>&1; then
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_PROVIDER="adc"
    else
        echo "Error: ADC token lacks the userinfo.email scope." >&2
        exit 1
    fi
else
    echo "Error: No usable auth provider found." >&2
    exit 1
fi
AUTH_FLAGS="--auth=$AUTH_PROVIDER"

server_endpoints() {
    # Isolate the listing from the user's normal sessions.json. The command
    # reads server assignments but cannot adopt, rename, or rewrite any of the
    # user's pre-existing local bindings.
    uv run colab $AUTH_FLAGS --config "$SERVER_SESSION_FILE" sessions 2>/dev/null | sed -n 's/^\[[^]]*\] \([^ ]*\) .*/\1/p' | sort
}

BEFORE_ENDPOINTS=$(server_endpoints)

cleanup() {
    cleanup_endpoint="$TEST_ENDPOINT"
    if [ -z "$cleanup_endpoint" ] && [ -s "$SESSION_FILE" ]; then
        cleanup_endpoint=$(uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY' 2>/dev/null || true
import json
import sys

print(json.load(open(sys.argv[1])).get(sys.argv[2], {}).get("endpoint", ""))
PY
        )
    fi

    # Always try the isolated local binding: `new` may have succeeded even if
    # the immediately-following endpoint read failed under `set -e`.
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" >/dev/null 2>&1 || true
    if [ -n "$cleanup_endpoint" ]; then
        # Exact, idempotent fallback. Do not gate cleanup on another assignments
        # listing: that lookup may be the failure that triggered this trap.
        uv run python - "$AUTH_PROVIDER" "$cleanup_endpoint" <<'PY' >/dev/null 2>&1 || true
import sys
from colab_cli.auth import AuthProvider
from colab_cli.common import state

state.auth_provider = AuthProvider(sys.argv[1])
state.client.unassign(sys.argv[2])
PY
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[*] Creating isolated CPU session $SESSION_NAME..."
# Intentionally omit --gpu/--tpu: this regression must never consume an
# accelerator allocation.
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"
TEST_ENDPOINT=$(uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1]))[sys.argv[2]]["endpoint"])
PY
)

expire_saved_token() {
    uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY'
import json
import sys

path, name = sys.argv[1:]
with open(path) as f:
    data = json.load(f)
data[name]["token"] = "deliberately-expired-runtime-proxy-token"
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PY
}

expire_saved_token
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" ls -s "$SESSION_NAME" content >/dev/null

expire_saved_token
EXEC_OUT=$(echo 'print("TOKEN-REFRESH-EXEC-OK")' | uv run colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME")
echo "$EXEC_OUT" | grep -q "TOKEN-REFRESH-EXEC-OK"

expire_saved_token
CONSOLE_OUT="$TMP_DIR/console.out"
timeout 30 bash -c "echo 'echo TOKEN-REFRESH-CONSOLE-OK' | uv run colab $AUTH_FLAGS --config '$SESSION_FILE' console -s '$SESSION_NAME'" >"$CONSOLE_OUT" 2>&1
grep -a -q "TOKEN-REFRESH-CONSOLE-OK" "$CONSOLE_OUT"

uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME"

AFTER_ENDPOINTS=$(server_endpoints)
if [ "$AFTER_ENDPOINTS" != "$BEFORE_ENDPOINTS" ]; then
    echo "[FAILURE] Pre-existing assignments changed during the test." >&2
    echo "Before:" >&2
    echo "$BEFORE_ENDPOINTS" >&2
    echo "After:" >&2
    echo "$AFTER_ENDPOINTS" >&2
    exit 1
fi
TEST_ENDPOINT=""

echo "[SUCCESS] Runtime token refresh works for ls, exec, and console on CPU."
