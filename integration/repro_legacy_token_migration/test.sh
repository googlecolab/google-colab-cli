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
# Integration Test: Legacy token.json -> keyring migration
#
# Verifies how `colab --auth=oauth2` handles a pre-keyring plaintext token at
# ~/.config/colab-cli/token.json:
#   1. Keyring empty:      token is used, copied into the keyring, file deleted.
#   2. Keyring has token:  keyring token is kept, file deleted.
#   3. Keyring broken:     token is used, file kept, warning printed to stderr.
#
# Runs against a throwaway HOME and a file-backed test keyring, so the real
# token and OS keyring are never touched. The token is fake (unexpired), so the
# backend request after authentication fails; only the local token handling is
# asserted. No VM is allocated.
#

set -u

# Keep uv's cache when HOME is overridden below.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

TEST_HOME="$TMP_DIR/home"
LEGACY="$TEST_HOME/.config/colab-cli/token.json"
KEYRING_FILE="$TMP_DIR/keyring.json"
KEY="google-colab-cli/oauth_token"
mkdir -p "$TMP_DIR/mods"

cat > "$TMP_DIR/mods/filekeyring.py" <<'EOF'
import json
import os

from keyring.backend import KeyringBackend

PATH = os.environ["FILE_KEYRING_PATH"]


class Keyring(KeyringBackend):
    priority = 1

    def _load(self):
        if not os.path.exists(PATH):
            return {}
        with open(PATH) as f:
            return json.load(f)

    def get_password(self, service, username):
        return self._load().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        data = self._load()
        data[f"{service}/{username}"] = password
        with open(PATH, "w") as f:
            json.dump(data, f)

    def delete_password(self, service, username):
        pass
EOF

fake_token() {
    echo "{\"token\": \"$1\", \"refresh_token\": \"r\", \"client_id\": \"c\", \"client_secret\": \"s\", \"token_uri\": \"https://oauth2.googleapis.com/token\", \"expiry\": \"2099-01-01T00:00:00Z\"}"
}

keyring_access_token() {
    python3 -c "import json, sys; d = json.load(open(sys.argv[1])); print(json.loads(d[sys.argv[2]])['token'])" "$KEYRING_FILE" "$KEY" 2>/dev/null
}

reset_state() {
    rm -rf "$TEST_HOME" "$KEYRING_FILE"
    mkdir -p "$(dirname "$LEGACY")"
    fake_token "legacy" > "$LEGACY"
}

# Runs the CLI with the given keyring backend; output goes to $OUT.
run_colab() {
    OUT="$TMP_DIR/out.log"
    HOME="$TEST_HOME" PYTHONPATH="$TMP_DIR/mods" FILE_KEYRING_PATH="$KEYRING_FILE" \
        PYTHON_KEYRING_BACKEND="$1" PYTHONUNBUFFERED=1 \
        timeout 60 uv run colab --auth=oauth2 sessions < /dev/null > "$OUT" 2>&1
}

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

echo "[*] Scenario 1: keyring empty, legacy token.json present"
reset_state
run_colab filekeyring.Keyring
check "no OAuth prompt" '! grep -q "To authorize colab-cli" "$OUT"'
check "legacy token copied into keyring" '[ "$(keyring_access_token)" = "legacy" ]'
check "legacy token.json deleted" '[ ! -f "$LEGACY" ]'

echo "[*] Scenario 2: keyring already has a token, legacy token.json present"
reset_state
python3 -c "import json, sys; json.dump({sys.argv[2]: sys.argv[3]}, open(sys.argv[1], 'w'))" \
    "$KEYRING_FILE" "$KEY" "$(fake_token keyring)"
run_colab filekeyring.Keyring
check "no OAuth prompt" '! grep -q "To authorize colab-cli" "$OUT"'
check "keyring token unchanged" '[ "$(keyring_access_token)" = "keyring" ]'
check "legacy token.json deleted" '[ ! -f "$LEGACY" ]'

echo "[*] Scenario 3: keyring inaccessible, legacy token.json present"
reset_state
run_colab keyring.backends.fail.Keyring
check "no OAuth prompt" '! grep -q "To authorize colab-cli" "$OUT"'
check "keyring save warning on stderr" 'grep -q "Could not save the OAuth token to the OS keyring" "$OUT"'
check "legacy token.json kept" '[ -f "$LEGACY" ]'

if [ "$FAILURES" -ne 0 ]; then
    echo "[FAILURE] $FAILURES check(s) failed."
    exit 1
fi
echo "[SUCCESS] Legacy token.json migration behaves as expected."
