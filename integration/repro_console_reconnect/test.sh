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

# CPU-only live regression for interactive Console reconnects. The probe
# establishes genuine /colab/tty WebSockets and exercises two recovery paths:
# a transient control-plane proxy failure falls back to the last token, and a
# binding removed by another process stops without another HTTP lookup. Existing
# assignments are only snapshotted and must be unchanged after the isolated test
# assignment is removed.

set -euo pipefail

TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
SERVER_SESSION_FILE="$TMP_DIR/server-snapshot.json"
OUTPUT_FILE="$TMP_DIR/console.out"
STOP_SESSION_FILE="$TMP_DIR/stop-sessions.json"
STOP_OUTPUT_FILE="$TMP_DIR/console-stop.out"
SESSION_NAME="console-reconnect-$PPID-$$"
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

    # `new` may have succeeded even if the first endpoint read failed. Always
    # stop by the isolated name, then unassign the exact recovered endpoint.
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
        echo "[FAILURE] Captured Console probe output:" >&2
        sed 's/^/  /' "$OUTPUT_FILE" >&2
    fi
    if [ "$exit_code" -ne 0 ] && [ -s "$STOP_OUTPUT_FILE" ]; then
        echo "[FAILURE] Captured local-stop probe output:" >&2
        sed 's/^/  /' "$STOP_OUTPUT_FILE" >&2
    fi
    rm -rf "$TMP_DIR"
    trap - EXIT
    exit "$exit_code"
}
trap cleanup EXIT

echo "[*] Creating isolated CPU session $SESSION_NAME..."
# Deliberately omit --gpu/--tpu. This test must never consume an accelerator or
# interact with a pre-existing accelerator assignment.
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

TEST_ENDPOINT=$(uv run python - "$SESSION_FILE" "$SESSION_NAME" <<'PY'
import json
import sys

session = json.load(open(sys.argv[1]))[sys.argv[2]]
print(session["endpoint"])
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

echo "[*] Injecting one transient refresh failure on CPU endpoint $TEST_ENDPOINT..."
timeout 120 uv run python - "$AUTH_PROVIDER" "$SESSION_FILE" "$SESSION_NAME" >"$OUTPUT_FILE" 2>&1 <<'PY'
import os
import pty
import sys
import threading

import colab_cli.console as console
from colab_cli.auth import AuthProvider
from colab_cli.common import State
from requests.exceptions import ProxyError

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
created = 0
second_open = threading.Event()
forced_disconnect = threading.Event()
refresh_failures = 0


def websocket_app(**kwargs):
    global created
    created += 1
    attempt_number = created
    original_open = kwargs["on_open"]

    def injected_open(ws):
        original_open(ws)
        if attempt_number == 1:
            def disconnect():
                forced_disconnect.set()
                ws.sock.shutdown()

            threading.Timer(1.0, disconnect).start()
        elif attempt_number == 2:
            second_open.set()

    kwargs["on_open"] = injected_open
    return real_websocket_app(**kwargs)


master_fd, slave_fd = pty.openpty()
original_stdin = sys.stdin
stdin_stream = os.fdopen(slave_fd, encoding="utf-8", buffering=1)
sys.stdin = stdin_stream
console.websocket.WebSocketApp = websocket_app


def drive_reconnected_shell():
    if not second_open.wait(90):
        os.write(master_fd, b"\x03")
        return
    os.write(master_fd, b"printf 'COLAB_CONSOLE_RECONNECT_OK\\n'\nexit\n")


def refresh_session(expected):
    global refresh_failures
    if refresh_failures == 0:
        refresh_failures += 1
        raise ProxyError("injected transient control-plane proxy failure")
    return state.refresh_session(
        session_name, expected_session=expected, timeout=10
    )


threading.Thread(target=drive_reconnected_shell, daemon=True).start()
try:
    console.connect_console(
        session,
        refresh_session=refresh_session,
        retry_delays=(1,),
        _max_reconnect_attempts=2,
    )
finally:
    console.websocket.WebSocketApp = real_websocket_app
    sys.stdin = original_stdin
    stdin_stream.close()
    os.close(master_fd)

if not forced_disconnect.is_set():
    raise SystemExit("Fault injection did not run")
if not second_open.is_set():
    raise SystemExit("Console did not reconnect")
print(
    f"INTEGRATION_OK attempts={created} refresh_failures={refresh_failures} "
    f"endpoint={session.endpoint}"
)
PY

grep -a -q "Console connection lost" "$OUTPUT_FILE"
grep -a -q "Reconnecting in 1s (attempt 1" "$OUTPUT_FILE"
grep -a -q "could not refresh credentials" "$OUTPUT_FILE"
grep -a -q "Console reconnected (attempt 1, endpoint $TEST_ENDPOINT)" "$OUTPUT_FILE"
grep -a -q "COLAB_CONSOLE_RECONNECT_OK" "$OUTPUT_FILE"
grep -a -q "INTEGRATION_OK attempts=2 refresh_failures=1 endpoint=$TEST_ENDPOINT" "$OUTPUT_FILE"

echo "[*] Simulating a completed concurrent stop in an isolated binding copy..."
cp "$SESSION_FILE" "$STOP_SESSION_FILE"
timeout 60 uv run python - "$AUTH_PROVIDER" "$STOP_SESSION_FILE" "$SESSION_NAME" >"$STOP_OUTPUT_FILE" 2>&1 <<'PY'
import os
import pty
import subprocess
import sys
import threading

import colab_cli.console as console
from colab_cli.auth import AuthProvider
from colab_cli.commands.execution import _refresh_console_session
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
binding_removed = threading.Event()
refresh_calls = 0


def websocket_app(**kwargs):
    original_open = kwargs["on_open"]

    def injected_open(ws):
        original_open(ws)

        def remove_binding_and_disconnect():
            code = (
                "import sys; from colab_cli.state import StateStore; "
                "removed = StateStore(sys.argv[1]).remove_if_endpoint("
                "sys.argv[2], sys.argv[3]); raise SystemExit(removed is None)"
            )
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    config_path,
                    session_name,
                    session.endpoint,
                ],
                check=True,
            )
            binding_removed.set()
            ws.sock.shutdown()

        threading.Timer(1.0, remove_binding_and_disconnect).start()

    kwargs["on_open"] = injected_open
    return real_websocket_app(**kwargs)


def refresh_session(expected):
    global refresh_calls
    refresh_calls += 1
    return _refresh_console_session(state, session_name, expected)


master_fd, slave_fd = pty.openpty()
original_stdin = sys.stdin
stdin_stream = os.fdopen(slave_fd, encoding="utf-8", buffering=1)
sys.stdin = stdin_stream
console.websocket.WebSocketApp = websocket_app
try:
    console.connect_console(
        session,
        refresh_session=refresh_session,
        retry_delays=(1,),
        _max_reconnect_attempts=1,
    )
finally:
    console.websocket.WebSocketApp = real_websocket_app
    sys.stdin = original_stdin
    stdin_stream.close()
    os.close(master_fd)

if not binding_removed.is_set():
    raise SystemExit("Concurrent binding removal did not run")
if refresh_calls != 1:
    raise SystemExit(f"Expected one local refresh check, got {refresh_calls}")
if state._client is not None:
    raise SystemExit("Local stop unexpectedly initialized the HTTP client")
print(f"LOCAL_STOP_OK refresh_calls={refresh_calls} endpoint={session.endpoint}")
PY

grep -a -q "Session '$SESSION_NAME' is no longer active" "$STOP_OUTPUT_FILE"
grep -a -q "LOCAL_STOP_OK refresh_calls=1 endpoint=$TEST_ENDPOINT" "$STOP_OUTPUT_FILE"

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

echo "[SUCCESS] Console visibly reconnected to the same CPU endpoint."
