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

set -euo pipefail

export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"

TMP_DIR=$(mktemp -d)
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

TEST_HOME="$TMP_DIR/home"
SERVE_DIR="$TMP_DIR/www"
SETTINGS="$TEST_HOME/.config/colab-cli/settings.json"
OUT="$TMP_DIR/out.log"
mkdir -p "$TEST_HOME/.config/colab-cli" "$SERVE_DIR"

FAILURES=0
check() {
    if eval "$2"; then
        echo "  [PASS] $1"
    else
        echo "  [FAIL] $1"
        FAILURES=$((FAILURES + 1))
        echo "  ---- CLI output ----"
        sed 's/^/  | /' "$OUT"
    fi
}

echo "[*] Scenario 1: live PyPI + live GitHub default notices_url (404 when tag absent)"
HOME="$TEST_HOME" PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    uv run colab update > "$OUT" 2>&1
check "no warning on missing notices tag" '! grep -q "Warning:" "$OUT"'
check "settings.json persisted empty update_notices" \
    'python3 -c "import json, sys; assert json.load(open(sys.argv[1]))[\"update_notices\"] == []" "$SETTINGS"'

PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()")
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$SERVE_DIR" >/dev/null 2>&1 &
SERVER_PID=$!

INSTALLED_VER=$(HOME="$TEST_HOME" uv run colab version | awk '{print $2}')
cat > "$SERVE_DIR/pypi.json" <<EOF
{"info": {"version": "$INSTALLED_VER"}}
EOF
cat > "$SERVE_DIR/notices.json" <<'EOF'
[
  {"affects": ">=0.0.0", "message": "Standalone advisory from movable notices tag"},
  {"affects": "<0.0.1", "message": "Unmatched older notice"}
]
EOF
cat > "$SETTINGS" <<EOF
{
  "update_url": "http://127.0.0.1:$PORT/pypi.json",
  "notices_url": "http://127.0.0.1:$PORT/notices.json"
}
EOF

echo "[*] Scenario 2: standalone notice shown when up to date + cached across invocations"
HOME="$TEST_HOME" PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    uv run colab update > "$OUT" 2>&1
check "reports up to date" 'grep -q "Colab CLI is up to date" "$OUT"'
check "prints matching standalone notice" \
    'grep -q "\[colab\] Notice: Standalone advisory from movable notices tag" "$OUT"'
check "omits unmatched notice" '! grep -q "Unmatched older notice" "$OUT"'
check "explicit update omits silence hint" '! grep -q "To silence this check" "$OUT"'

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""

HOME="$TEST_HOME" PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    uv run colab stop < /dev/null > "$OUT" 2>&1 || true
check "throttled command prints cached standalone notice with server offline" \
    'grep -q "\[colab\] Notice: Standalone advisory from movable notices tag" "$OUT"'
check "throttled unsolicited notice includes silence hint" \
    'grep -q "To silence this check" "$OUT"'

HOME="$TEST_HOME" PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    uv run colab version < /dev/null > "$OUT" 2>&1
check "suppressed subcommand omits standalone notice" \
    '! grep -q "Standalone advisory" "$OUT"'

echo "[*] Scenario 3: deleting notices.json (HTTP 404) retracts cached notices"
rm -f "$SERVE_DIR/notices.json"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$SERVE_DIR" >/dev/null 2>&1 &
SERVER_PID=$!

HOME="$TEST_HOME" PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    uv run colab update < /dev/null > "$OUT" 2>&1
check "404 retraction emits no warning" '! grep -q "Warning:" "$OUT"'
check "retracted notice no longer printed" '! grep -q "Standalone advisory" "$OUT"'

HOME="$TEST_HOME" PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    uv run colab stop < /dev/null > "$OUT" 2>&1 || true
check "cached notice cleared for subsequent commands" \
    '! grep -q "Standalone advisory" "$OUT"'

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] Standalone update notices integration test passed."
