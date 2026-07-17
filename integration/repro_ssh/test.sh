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

# Integration Test: `colab ssh`
#
# Fast, always-runnable part (smoke): verifies the subcommand is wired into the
# CLI and its help renders with the documented options.
#
# Live part (opt-in via RUN_LIVE=1): the real end-to-end flow requires a runtime
# that exposes the SSH-over-WebSocket endpoint (`/colab/ssh`) AND an interactive
# TTY, so it CANNOT run headlessly and is left as a documented manual scenario.

set -u

echo "== smoke: colab ssh is registered and --help renders =="
HELP="$(uv run colab ssh --help 2>&1)"
echo "$HELP"

fail=0
for needle in "--proxy-mode" "--identity" "--session" "Connect to a Colab runtime via SSH"; do
    if ! echo "$HELP" | grep -q -- "$needle"; then
        echo "FAIL: '$needle' missing from 'colab ssh --help'"
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "SMOKE FAILED"
    exit 1
fi
echo "SMOKE PASSED"

if [ "${RUN_LIVE:-0}" != "1" ]; then
    cat <<'EOF'

== live scenario (skipped; set RUN_LIVE=1 to run manually) ==
Requires a runtime that exposes the SSH endpoint. Manual steps:
  1. colab new -s sshtest
  2. colab ssh -s sshtest            # interactive; `whoami` should print root
  3. Error modes (each should print an actionable message):
       - no key:   mv ~/.ssh/id_*.pub /tmp; colab ssh -s sshtest  -> "no SSH public key found"
       - 429:      open a second `colab ssh -s sshtest`           -> "Already-active SSH session"
       - 404:      target a runtime without the SSH endpoint      -> "Endpoint not found"
  4. Proxy mode: ssh -o ProxyCommand="colab ssh --proxy-mode -s sshtest" root@colab-runtime
  5. colab stop -s sshtest ; colab sessions   # confirm no orphan VMs
EOF
    exit 0
fi

echo "== live: RUN_LIVE=1 set, but interactive ssh cannot be driven headlessly =="
echo "Run the manual steps above in a real terminal."
exit 0
