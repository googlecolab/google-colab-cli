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

# CPU-only live regression for the raw /colab/tty application-level flow
# control. The runtime requests an acknowledgement roughly every 100 KB and
# pauses its PTY after six missed acknowledgements, so a short Console smoke
# test cannot expose the failure.

set -euo pipefail

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SERVER_SESSION_FILE="$TMP_DIR/server-snapshot.json"
OUTPUT_FILE="$TMP_DIR/console.out"
SESSION_NAME="console-flow-control-$PPID-$$"
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
    uv run colab $AUTH_FLAGS --config "$SERVER_SESSION_FILE" sessions 2>/dev/null | sed -n 's/^\[[^]]*\] \([^ ]*\) .*/\1/p' | sort
}

BEFORE_ENDPOINTS=$(server_endpoints)

cleanup() {
    exit_code=$?
    cleanup_endpoint="$TEST_ENDPOINT"
    if [ -z "$cleanup_endpoint" ] && [ -s "$SESSION_FILE" ]; then
        cleanup_endpoint=$(uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY' 2>/dev/null || true
import json
import sys

print(json.load(open(sys.argv[1])).get(sys.argv[2], {}).get("endpoint", ""))
PY
        )
    fi

    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" >/dev/null 2>&1 || true
    if [ -n "$cleanup_endpoint" ]; then
        uv run python - "$AUTH_PROVIDER" "$cleanup_endpoint" <<'PY' >/dev/null 2>&1 || true
import sys
from colab_cli.auth import AuthProvider
from colab_cli.common import state

state.auth_provider = AuthProvider(sys.argv[1])
state.client.unassign(sys.argv[2])
PY
    fi
    if [ "$exit_code" -ne 0 ] && [ -s "$OUTPUT_FILE" ]; then
        echo "[FAILURE] Last 1024 bytes of Console output:" >&2
        tail -c 1024 "$OUTPUT_FILE" >&2
    fi
    rm -rf "$TMP_DIR"
    trap - EXIT
    exit "$exit_code"
}
trap cleanup EXIT

echo "[*] Creating isolated CPU session $SESSION_NAME..."
# Deliberately omit --gpu/--tpu. This test must not consume or reconnect to an
# accelerator assignment.
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

TEST_ENDPOINT=$(uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1]))[sys.argv[2]]["endpoint"])
PY
)
TEST_ACCELERATOR=$(uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1]))[sys.argv[2]]["accelerator"])
PY
)
if [ "$TEST_ACCELERATOR" != "NONE" ]; then
    echo "[FAILURE] Refusing to probe non-CPU session: $TEST_ACCELERATOR" >&2
    exit 1
fi

echo "[*] Streaming beyond the PTY pause threshold on $TEST_ENDPOINT..."
timeout 120 uv run python - "$AUTH_PROVIDER" "$SESSION_FILE" "$SESSION_NAME" >"$OUTPUT_FILE" 2>&1 <<'PY'
import json
import os
import pty
import sys
import threading

import colab_cli.console as console
from colab_cli.auth import AuthProvider
from colab_cli.common import State

auth_provider, config_path, session_name = sys.argv[1:]
state = State()
state.auth_provider = AuthProvider(auth_provider)
state.config_path = config_path
session = state.store.get(session_name)
if session is None:
    raise SystemExit("Isolated CPU session is missing")
if str(session.accelerator) not in ("AcceleratorType.NONE", "NONE"):
    raise SystemExit(f"Refusing to probe non-CPU session: {session.accelerator}")

real_websocket_app = console.websocket.WebSocketApp
real_forwarder = console._ConsoleInputForwarder
opened = threading.Event()
sentinel_received = threading.Event()
ack_requests = 0
active_ws = None
active_forwarder = None
output_tail = ""


class ObservedForwarder(real_forwarder):
    def __init__(self, *args, **kwargs):
        global active_forwarder
        super().__init__(*args, **kwargs)
        active_forwarder = self


def websocket_app(**kwargs):
    original_open = kwargs["on_open"]
    original_message = kwargs["on_message"]

    def observed_open(ws):
        global active_ws
        original_open(ws)
        active_ws = ws
        opened.set()

    def observed_message(ws, message):
        global ack_requests, output_tail
        saw_sentinel = False
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            pass
        else:
            if payload.get("ack") is True:
                ack_requests += 1
            output_tail = (output_tail + payload.get("data", ""))[-64:]
            if "CONSOLE_FLOW_CONTROL_OK" in output_tail:
                saw_sentinel = True
        original_message(ws, message)
        if saw_sentinel:
            sentinel_received.set()

    kwargs["on_open"] = observed_open
    kwargs["on_message"] = observed_message
    return real_websocket_app(**kwargs)


master_fd, slave_fd = pty.openpty()
original_stdin = sys.stdin
stdin_stream = os.fdopen(slave_fd, encoding="utf-8", buffering=1)
sys.stdin = stdin_stream
console.websocket.WebSocketApp = websocket_app
console._ConsoleInputForwarder = ObservedForwarder


def drive_shell():
    if not opened.wait(60):
        os.write(master_fd, b"\x03")
        return
    command = (
        b"python3 -c \"import sys,time; "
        b"[(sys.stdout.write('F' * 1024 + '\\\\n'), sys.stdout.flush(), "
        b"time.sleep(0.02)) for _ in range(800)]; "
        b"print('CONSOLE_FLOW_' + 'CONTROL_OK')\"\n"
    )
    os.write(master_fd, command)
    if not sentinel_received.wait(90):
        os.write(master_fd, b"\x03")
        return
    if active_forwarder is not None:
        active_forwarder.user_requested_close = True
    if active_ws is not None:
        active_ws.close()


threading.Thread(target=drive_shell, daemon=True).start()
try:
    console.connect_console(
        session,
        refresh_session=lambda expected: state.refresh_session(
            session_name, expected_session=expected, timeout=10
        ),
        retry_delays=(1,),
        _max_reconnect_attempts=1,
    )
finally:
    console.websocket.WebSocketApp = real_websocket_app
    console._ConsoleInputForwarder = real_forwarder
    sys.stdin = original_stdin
    stdin_stream.close()
    os.close(master_fd)

if ack_requests < 6:
    raise SystemExit(f"Expected at least 6 PTY ACK requests, got {ack_requests}")
print(f"CONSOLE_FLOW_CONTROL_ACK_REQUESTS={ack_requests}")
PY

grep -a -q "CONSOLE_FLOW_CONTROL_OK" "$OUTPUT_FILE"
grep -a -E -q "CONSOLE_FLOW_CONTROL_ACK_REQUESTS=([6-9]|[1-9][0-9]+)" "$OUTPUT_FILE"
OUTPUT_BYTES=$(wc -c < "$OUTPUT_FILE")
if [ "$OUTPUT_BYTES" -lt 650000 ]; then
    echo "[FAILURE] Console returned only $OUTPUT_BYTES bytes." >&2
    exit 1
fi

echo "[*] Stopping isolated CPU session..."
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

echo "[SUCCESS] Console acknowledged PTY flow control beyond 600 KB on CPU."
