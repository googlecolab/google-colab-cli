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
# Integration Test: Fallback OAuth Configuration Verification
#
# This test verifies that the CLI correctly falls back to using the bundled
# oauth_config.json containing the default client ID when no local config is present.
# It simulates a fresh user flow by temporarily hiding an existing config and
# the keyring token, starting the OAuth flow, and asserting that the printed
# authorization URL contains the correct Client ID.
#

set -e

CONFIG_PATH="$HOME/.colab-cli-oauth-config.json"
BACKUP_SUFFIX=".backup.$(date +%s)"

CONFIG_BACKUP=""

cleanup() {
    echo "[*] Cleaning up backups..."
    if [ -n "$CONFIG_BACKUP" ] && [ -f "$CONFIG_BACKUP" ]; then
        mv "$CONFIG_BACKUP" "$CONFIG_PATH"
        echo "[*] Restored .colab-cli-oauth-config.json"
    fi
}
trap cleanup EXIT

# 1. Back up the existing client config if it exists
if [ -f "$CONFIG_PATH" ]; then
    CONFIG_BACKUP="${CONFIG_PATH}${BACKUP_SUFFIX}"
    mv "$CONFIG_PATH" "$CONFIG_BACKUP"
    echo "[*] Temporarily backed up .colab-cli-oauth-config.json to $CONFIG_BACKUP"
fi

# 2. Run colab sessions command to trigger the OAuth flow
echo "[*] Running 'colab --auth=oauth2 sessions' (expecting to trigger browser flow)..."
# We expect the command to block waiting for authorization, so we run it with a timeout.
# We redirect output to a file so we can inspect it.
OUTPUT_LOG=$(mktemp)
set +e
# The null keyring backend hides any token cached in the host's OS keyring.
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    PYTHONUNBUFFERED=1 timeout 5 uv run colab --auth=oauth2 sessions > "$OUTPUT_LOG" 2>&1
EXIT_CODE=$?
set -e

echo "[*] Command exited with code: $EXIT_CODE"
echo "----------------- CLI Output -----------------"
cat "$OUTPUT_LOG"
echo "----------------------------------------------"

# 3. Assertions
EXPECTED_CLIENT_ID="764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"

# The command should have printed the authorization URL
if ! grep -q "To authorize colab-cli, visit this URL" "$OUTPUT_LOG"; then
    echo "[FAILURE] OAuth prompt message not found in output."
    rm -f "$OUTPUT_LOG"
    exit 1
fi

# The URL should contain the correct client ID
if ! grep -q "client_id=$EXPECTED_CLIENT_ID" "$OUTPUT_LOG"; then
    echo "[FAILURE] Authorization URL does not contain the expected client ID: $EXPECTED_CLIENT_ID"
    rm -f "$OUTPUT_LOG"
    exit 1
fi

echo "[SUCCESS] Verified that CLI correctly initiated OAuth flow with the fallback Client ID: $EXPECTED_CLIENT_ID"
rm -f "$OUTPUT_LOG"
