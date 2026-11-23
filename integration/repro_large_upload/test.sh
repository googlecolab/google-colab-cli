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

# Integration Test: `colab upload` of a file too large for one request
#
# Colab runtimes reject Contents API request bodies over 100 MiB, so files
# larger than about 75 MiB (base64 inside JSON) are uploaded in chunks.
#   1. A 100 MiB file uploads in chunks and its remote sha256 matches.
#   2. Uploading a 1 MiB file to the same path replaces it completely.
#   3. The session this test created is stopped (other sessions are left alone).

# Don't `set -e` so we can capture failures and clean up explicitly.

# ---------- Auth detection ----------------------------------------------------
has_oauth2_token() {
    uv run python -c "import sys, keyring; sys.exit(0 if keyring.get_password('google-colab-cli', 'oauth_token') else 1)" > /dev/null 2>&1
}
if has_oauth2_token; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    AUTH_FLAGS="--auth=adc"
else
    echo "Error: No usable auth provider found."
    exit 1
fi
echo "[*] Using $AUTH_FLAGS"

# ---------- Isolated session state -------------------------------------------
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SESSION_NAME="repro-large-upload-$(date +%s)"
REMOTE_PATH="/content/repro_large_upload.bin"
BIG_FILE="$TMP_DIR/big.bin"
SMALL_FILE="$TMP_DIR/small.bin"
VERIFY_SCRIPT="$TMP_DIR/verify.py"

colab_cmd() {
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" "$@"
}

cleanup() {
    echo "[*] Cleaning up..."
    colab_cmd stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

sha256_of() {
    uv run python -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" "$1"
}

cat > "$VERIFY_SCRIPT" <<PYEOF
import hashlib, os
path = "$REMOTE_PATH"
with open(path, "rb") as f:
    digest = hashlib.sha256(f.read()).hexdigest()
print(f"REMOTE size={os.path.getsize(path)} sha256={digest}")
PYEOF

expect_remote() {
    local local_file="$1" label="$2"
    local want_size want_sha out
    want_size=$(wc -c < "$local_file" | tr -d ' ')
    want_sha=$(sha256_of "$local_file")
    out=$(colab_cmd exec -s "$SESSION_NAME" --timeout 120 -f "$VERIFY_SCRIPT" 2>&1)
    echo "$out" | grep "REMOTE"
    if ! echo "$out" | grep -q "REMOTE size=$want_size sha256=$want_sha"; then
        echo "[FAILURE] $label: remote file does not match (want size=$want_size sha256=$want_sha)."
        exit 1
    fi
}

uv run python -c "import os, sys; open(sys.argv[1], 'wb').write(os.urandom(100 * 1024 * 1024))" "$BIG_FILE"
uv run python -c "import os, sys; open(sys.argv[1], 'wb').write(os.urandom(1024 * 1024))" "$SMALL_FILE"

echo "[*] Creating CPU session $SESSION_NAME"
if ! colab_cmd new -s "$SESSION_NAME"; then
    echo "[FAILURE] colab new failed."
    exit 1
fi

# ---------- Phase 1: chunked upload ------------------------------------------
echo "[*] Phase 1: colab upload of a 100 MiB file"
if ! colab_cmd upload -s "$SESSION_NAME" "$BIG_FILE" "$REMOTE_PATH"; then
    echo "[FAILURE] Uploading the 100 MiB file failed."
    exit 1
fi
expect_remote "$BIG_FILE" "Phase 1"
echo "[SUCCESS] Phase 1 passed: the 100 MiB file arrived intact."

# ---------- Phase 2: a small upload replaces the large file ------------------
echo "[*] Phase 2: colab upload of a 1 MiB file to the same path"
if ! colab_cmd upload -s "$SESSION_NAME" "$SMALL_FILE" "$REMOTE_PATH"; then
    echo "[FAILURE] Uploading the 1 MiB file failed."
    exit 1
fi
expect_remote "$SMALL_FILE" "Phase 2"
echo "[SUCCESS] Phase 2 passed: the small upload replaced the large file."

# ---------- Phase 3: no session left behind ----------------------------------
colab_cmd stop -s "$SESSION_NAME"
SESSIONS_OUT=$(colab_cmd sessions 2>&1)
echo "$SESSIONS_OUT"
if echo "$SESSIONS_OUT" | grep -q "\[$SESSION_NAME\]"; then
    echo "[FAILURE] Session $SESSION_NAME is still active after colab stop."
    exit 1
fi
echo "[SUCCESS] Phase 3 passed: the test session was stopped."
echo "[SUCCESS] All phases passed."
exit 0
