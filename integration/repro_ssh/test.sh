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
# Part A (offline, always runs; no VM): `--help` advertises the documented
#   flags, and an unknown session exits 2 with an actionable message.
#
# Part B (live; runs when auth is available; allocates a CPU VM): a genuine
#   end-to-end. `colab ssh --proxy-mode` is non-interactive, so we use it as an
#   OpenSSH ProxyCommand and run a real remote command over the WebSocket
#   bridge. This exercises the SAME connect -> pubkey-header -> handshake ->
#   bridge path as the interactive shell, minus the TTY:
#     colab new -> substrate check (sshd up) -> `ssh root@... "whoami"` over the
#     bridge -> assert it ran as root on the runtime -> RSA-key rejection ->
#     colab stop -> assert no orphan VM.

# Do not `set -e`: we capture failures explicitly so cleanup always runs.
set -u

# ---------- Part A: offline smoke (always runs, no VM) -----------------------
echo "== A1: colab ssh --help advertises the documented flags =="
HELP="$(uv run colab ssh --help 2>&1)"
echo "$HELP"

fail=0
for needle in "--proxy-mode" "--identity" "--session" "--gpu" "--tpu" "--rm" \
  "Connect to a Colab runtime via SSH"; do
  if ! printf '%s' "$HELP" | grep -q -- "$needle"; then
    echo "FAIL: '$needle' missing from 'colab ssh --help'"
    fail=1
  fi
done

echo "== A2: an unknown session exits 2 with an actionable message (offline) =="
OFFLINE_CFG="$(mktemp -d)/sessions.json"
A2_OUT="$(uv run colab --config "$OFFLINE_CFG" ssh -s ghost-no-such-session 2>&1)"
A2_RC=$?
echo "$A2_OUT"
if [ "$A2_RC" -ne 2 ]; then
  echo "FAIL: expected exit 2 for an unknown session, got $A2_RC"
  fail=1
fi
if ! printf '%s' "$A2_OUT" | grep -q "not found"; then
  echo "FAIL: expected a 'not found' message for an unknown session"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "OFFLINE SMOKE FAILED"
  exit 1
fi
echo "OFFLINE SMOKE PASSED"

# ---------- Part B: live end-to-end (needs auth + a VM) ----------------------
# Auth detection (mirrors integration/repro_run_command/test.sh).
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
  AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud >/dev/null && gcloud auth application-default print-access-token >/dev/null 2>&1; then
  ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
  ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
  if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
    AUTH_FLAGS="--auth=adc"
  else
    echo "[skip] live e2e: ADC token lacks the required scopes"
    echo "       (colaboratory + userinfo.email). Offline smoke passed."
    exit 0
  fi
else
  echo "[skip] live e2e: no usable auth provider (OAuth2 token or scoped ADC)."
  echo "       Offline smoke passed."
  exit 0
fi
echo "[*] Using $AUTH_FLAGS"

if ! command -v ssh >/dev/null || ! command -v ssh-keygen >/dev/null; then
  echo "[skip] live e2e: OpenSSH client (ssh/ssh-keygen) not found."
  exit 0
fi

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
KEY="$TMP_DIR/id_ed25519"
RSA_KEY="$TMP_DIR/id_rsa"
SESSION_NAME="repro-ssh-$(date +%s)"

cleanup() {
  echo "[*] Cleaning up..."
  uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" \
    2>/dev/null || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# Throwaway ed25519 key (RSA is server-rejected) so we never touch ~/.ssh.
ssh-keygen -t ed25519 -N "" -f "$KEY" >/dev/null

echo "[*] Creating runtime '$SESSION_NAME' (REAL API CALL)..."
if ! uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"; then
  echo "[FAILURE] colab new failed."
  exit 1
fi

echo "[*] Substrate check: is sshd listening on the runtime?"
SUB=$(
  cat <<'PY' | uv run colab $AUTH_FLAGS --config "$SESSION_FILE" exec -s "$SESSION_NAME" 2>&1
import subprocess
cmd = "pgrep -x sshd >/dev/null && echo SSHD_UP || echo SSHD_DOWN"
print(subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True).stdout.strip())
PY
)
echo "$SUB"
if ! echo "$SUB" | grep -q "SSHD_UP"; then
  echo "[FAILURE] sshd is not running on the runtime. The prod SSH substrate"
  echo "          (COLAB_ENABLE_SSH) is not present here, so the end-to-end"
  echo "          flow cannot succeed. This is an environment/prod issue, not"
  echo "          a client bug."
  exit 1
fi

echo "[*] End-to-end: run a remote command over --proxy-mode (non-interactive)."
MARKER="ssh-ok-$$-$RANDOM"
PROXY="uv run colab $AUTH_FLAGS --config $SESSION_FILE ssh --proxy-mode -s $SESSION_NAME -i $KEY"
E2E_OUT=$(
  timeout 120 ssh -F /dev/null \
    -o "ProxyCommand=$PROXY" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes \
    -o ConnectTimeout=60 \
    -o LogLevel=ERROR \
    -i "$KEY" \
    root@colab-runtime "whoami; echo $MARKER" 2>&1
)
E2E_RC=$?
echo "$E2E_OUT"
if [ "$E2E_RC" -ne 0 ]; then
  echo "[FAILURE] ssh over --proxy-mode exited $E2E_RC."
  exit 1
fi
if ! echo "$E2E_OUT" | grep -qx "root"; then
  echo "[FAILURE] remote 'whoami' did not report root."
  exit 1
fi
if ! echo "$E2E_OUT" | grep -q "$MARKER"; then
  echo "[FAILURE] remote marker '$MARKER' missing — the command did not run"
  echo "          on the runtime."
  exit 1
fi
echo "[SUCCESS] Handshake + pubkey-auth + bridge + remote exec all work."

echo "[*] Negative: an RSA key is rejected by the server (non-interactive)."
ssh-keygen -t rsa -b 2048 -N "" -f "$RSA_KEY" >/dev/null
RSA_OUT=$(
  uv run colab $AUTH_FLAGS --config "$SESSION_FILE" ssh --proxy-mode \
    -s "$SESSION_NAME" -i "$RSA_KEY" </dev/null 2>&1
)
echo "$RSA_OUT"
if ! echo "$RSA_OUT" | grep -qiE "unsupported key type|HTTP 400"; then
  echo "[FAILURE] RSA key did not surface the expected 'unsupported key type'"
  echo "          / HTTP 400 rejection."
  exit 1
fi
echo "[SUCCESS] RSA key correctly rejected with an actionable message."

echo "[*] Stopping session (REAL API CALL)..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME"
SESSIONS_OUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" sessions 2>&1)
echo "$SESSIONS_OUT"
if ! echo "$SESSIONS_OUT" | grep -q "No active sessions found on server."; then
  echo "[FAILURE] After stop, the server still reports active sessions"
  echo "          (possible orphan VM — investigate)."
  exit 1
fi

echo "[SUCCESS] All live SSH end-to-end checks passed."
exit 0
