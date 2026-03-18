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

set -e

# Integration Test: Plot Redirection
# Verifies that 'colab exec --output-image' correctly intercepts and saves plots.

SESSION_NAME="repro-plot-$(date +%s)"
OUTPUT_FILE="intercepted_plot.png"
SCRIPT_FILE="plot_gen.py"

# Cleanup on exit
cleanup() {
    echo "[*] Cleaning up..."
    colab stop -s "$SESSION_NAME" || true
    rm -f "$SCRIPT_FILE" "$OUTPUT_FILE"
}
trap cleanup EXIT

echo "[*] Creating script..."
cat <<EOF > "$SCRIPT_FILE"
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 10, 100)
y = np.sin(x)

plt.figure(figsize=(8, 4))
plt.plot(x, y)
plt.title("Repro Plot")
plt.show()
EOF

echo "[*] Starting session..."
colab new -s "$SESSION_NAME"

echo "[*] Running execution with plot redirection..."
# Note: On a fresh VM, this may trigger the retry logic.
colab exec -s "$SESSION_NAME" -f "$SCRIPT_FILE" --output-image "$OUTPUT_FILE"

if [ -f "$OUTPUT_FILE" ]; then
    echo "[SUCCESS] Plot intercepted and saved to $OUTPUT_FILE"
    ls -l "$OUTPUT_FILE"
else
    echo "[FAILURE] Plot file not found!"
    exit 1
fi
