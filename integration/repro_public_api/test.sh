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

# Integration Test: End-to-end Colab Public API (`--public-api`) lifecycle
#
# Verifies:
#   1. `colab test-public-api` confirms Public API access.
#   2. `colab --public-api new` allocates a CPU runtime via LRO (`CreateRuntime`)
#      and persists `runtime_name` (`runtimes/r-...`) in `sessions.json`.
#   3. `colab --public-api sessions` and `status` list the session.
#   4. `colab --public-api exec` refreshes an expired token via `GetRuntime`
#      (`GET /v1beta/runtimes/{runtime_id}`) and executes Python on the kernel.
#   5. `colab --public-api usage` fetches CCU info (when Tunnel API is available).
#   6. `colab --public-api stop` deletes the runtime (`DeleteRuntime`) and leaves
#      zero active/orphaned assignments on both Public and Tunnel APIs.

AUTH_FLAGS=""
PROBE_OUT=""
for candidate in "--auth=oauth2" "--auth=adc"; do
    OUT=$(timeout 15 uv run colab "$candidate" test-public-api < /dev/null 2>&1)
    if echo "$OUT" | grep -q "Public API (colaboratory.googleapis.com): OK"; then
        AUTH_FLAGS="$candidate"
        PROBE_OUT="$OUT"
        break
    fi
done
if [ -z "$AUTH_FLAGS" ]; then
    echo "[SKIP/FAIL] Neither oauth2 nor adc has access to colaboratory.googleapis.com."
    echo "Run 'colab test-public-api' or configure an allowlisted OAuth client / ADC quota project."
    exit 1
fi
TUNNEL_OK=0
if echo "$PROBE_OUT" | grep -q "Tunnel API (colab.research.google.com):   OK"; then
    TUNNEL_OK=1
fi
echo "[*] Using $AUTH_FLAGS (tunnel_ok=$TUNNEL_OK)"

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION_NAME="repro-pub-$(date +%s)"

cleanup() {
    echo "[*] Cleaning up..."
    uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" stop -s "$SESSION_NAME" >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[*] Step 1: colab --public-api new -s $SESSION_NAME"
NEW_OUT=$(uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" new -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$NEW_OUT"
if [ $RC -ne 0 ] || ! echo "$NEW_OUT" | grep -q "Session READY."; then
    echo "[FAILURE] colab --public-api new failed (rc=$RC)"
    exit 1
fi

if ! grep -q '"runtime_name": "runtimes/r-' "$SESSION_FILE"; then
    echo "[FAILURE] runtime_name was not persisted in $SESSION_FILE:"
    cat "$SESSION_FILE"
    exit 1
fi

read -r RUNTIME_ID ENDPOINT < <(python3 -c '
import json, sys
s = next(iter(json.load(open(sys.argv[1])).values()))
rt = (s.get("runtime_name") or "").removeprefix("runtimes/")
ep = s.get("endpoint") or ""
print(rt, ep)
' "$SESSION_FILE")
if [ -z "$RUNTIME_ID" ]; then
    echo "[FAILURE] Failed to parse runtime_name from $SESSION_FILE"
    exit 1
fi

echo "[*] Step 2: colab --public-api sessions & status"
SESSIONS_OUT=$(uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" sessions 2>&1)
RC=$?
echo "$SESSIONS_OUT"
if [ $RC -ne 0 ] || ! echo "$SESSIONS_OUT" | grep -q "\[$SESSION_NAME\]"; then
    echo "[FAILURE] Expected [$SESSION_NAME] in colab --public-api sessions output (rc=$RC)."
    exit 1
fi

STATUS_OUT=$(uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" status -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$STATUS_OUT"
if [ $RC -ne 0 ] || ! echo "$STATUS_OUT" | grep -q "\[$SESSION_NAME\].*Status: IDLE"; then
    echo "[FAILURE] Unexpected colab --public-api status output (rc=$RC)."
    exit 1
fi

echo "[*] Step 3: force token refresh via GetRuntime + colab --public-api exec"
python3 -c '
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for v in d.values():
    v["token_expires_at"] = None
json.dump(d, open(p, "w"), indent=2)
' "$SESSION_FILE"

EXEC_OUT=$(echo "print(21 * 2)" | uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" exec -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$EXEC_OUT"
if [ $RC -ne 0 ] || ! echo "$EXEC_OUT" | grep -qx "42"; then
    echo "[FAILURE] colab --public-api exec failed (rc=$RC)"
    exit 1
fi

echo "[*] Step 4: colab --public-api usage"
if [ "$TUNNEL_OK" -eq 1 ]; then
    USAGE_OUT=$(uv run colab $AUTH_FLAGS --public-api usage 2>&1)
    RC=$?
    echo "$USAGE_OUT"
    if [ $RC -ne 0 ] || ! echo "$USAGE_OUT" | grep -q "Current balance:"; then
        echo "[FAILURE] colab --public-api usage failed (rc=$RC)"
        exit 1
    fi
else
    echo "[SKIP] Tunnel API unavailable; skipping colab usage (/tun/m/ccu-info)."
fi

if [ "$TUNNEL_OK" -eq 1 ] && [ -z "$ENDPOINT" ]; then
    uv run colab $AUTH_FLAGS --no-public-api --config "$SESSION_FILE" sessions >/dev/null 2>&1 || true
    ENDPOINT=$(python3 -c 'import json,sys; s=next(iter(json.load(open(sys.argv[1])).values()), {}); print(s.get("endpoint") or "")' "$SESSION_FILE")
fi

echo "[*] Step 5: colab --public-api stop -s $SESSION_NAME"
STOP_OUT=$(uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>&1)
RC=$?
echo "$STOP_OUT"
if [ $RC -ne 0 ] || ! echo "$STOP_OUT" | grep -q "Session terminated."; then
    echo "[FAILURE] colab --public-api stop failed (rc=$RC)"
    exit 1
fi

echo "[*] Step 6: verify runtime removed on Public (and Tunnel) API"
PUB_AFTER=$(uv run colab $AUTH_FLAGS --public-api --config "$SESSION_FILE" sessions 2>&1)
RC=$?
echo "$PUB_AFTER"
if [ $RC -ne 0 ] || echo "$PUB_AFTER" | grep -qF "$RUNTIME_ID"; then
    echo "[FAILURE] Runtime $RUNTIME_ID still active on Public API (or sessions failed, rc=$RC)."
    exit 1
fi
if [ -n "$ENDPOINT" ] && echo "$PUB_AFTER" | grep -qF "$ENDPOINT"; then
    echo "[FAILURE] Endpoint $ENDPOINT still active on Public API after stop."
    exit 1
fi
if [ "$TUNNEL_OK" -eq 1 ]; then
    TUN_AFTER=$(uv run colab $AUTH_FLAGS --no-public-api --config "$SESSION_FILE" sessions 2>&1)
    RC=$?
    echo "$TUN_AFTER"
    if [ $RC -ne 0 ]; then
        echo "[FAILURE] colab --no-public-api sessions failed after stop (rc=$RC)."
        exit 1
    fi
    if [ -n "$ENDPOINT" ] && echo "$TUN_AFTER" | grep -qF "$ENDPOINT"; then
        echo "[FAILURE] Endpoint $ENDPOINT still active on Tunnel API after stop."
        exit 1
    fi
fi

echo "[SUCCESS] Public API integration test passed."
exit 0
