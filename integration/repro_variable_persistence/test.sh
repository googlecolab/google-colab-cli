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

# set -e  # Don't exit on error so we can capture the failure

# Integration Test: Variable Persistence
# Verifies if variables defined in one 'colab exec' call persist in the next.

SESSION_NAME="repro-persist-$(date +%s)"

# Cleanup on exit
cleanup() {
    echo "[*] Cleaning up..."
    colab stop -s "$SESSION_NAME" || true
}
trap cleanup EXIT

echo "[*] Starting session..."
colab new -s "$SESSION_NAME"

echo "[*] Phase 1: Defining variable 'eric'..."
echo 'eric = "present"' | colab exec -s "$SESSION_NAME"

echo "[*] Phase 2: Attempting to access 'eric'..."
# If this fails, it will return exit code 0 but print a Traceback to stderr
OUTPUT=$(echo 'print(f"Value of eric: {eric}")' | colab exec -s "$SESSION_NAME" 2>&1)

echo "[*] Result:"
echo "$OUTPUT"

if echo "$OUTPUT" | grep -q "NameError: name 'eric' is not defined"; then
    echo "[FAILURE] Variable persistence failed (NameError detected)."
    exit 1
elif echo "$OUTPUT" | grep -q "Value of eric: present"; then
    echo "[SUCCESS] Variable persistence verified."
else
    echo "[UNKNOWN] Unexpected output format."
    exit 1
fi
