#!/bin/bash
set -e

# Setup a clean session file for testing
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
trap "rm -rf $TMP_DIR" EXIT

# Check for existing OAuth token or valid gcert
# Since we are in a non-interactive agent environment, 'gcert' will likely fail.
# If the user has a valid token.json, we can use --auth-oauth2.

AUTH_FLAGS="--auth-loas2"
if [ ! -x "$(command -v gcertstatus)" ] || ! gcertstatus --check_remaining=1m --quiet; then
    echo "Warning: gcert invalid or missing. Attempting to fallback to OAuth2 if token exists..."
    if [ -f "$HOME/.config/colab-cli/token.json" ]; then
        AUTH_FLAGS="--auth-oauth2"
    else
        echo "Error: No valid gcert and no ~/.config/colab-cli/token.json found."
        echo "Please run 'gcert' or 'colab auth' in a real terminal before running this test."
        exit 1
    fi
fi

echo "Creating new session (REAL API CALL) using $AUTH_FLAGS..."
# We use 'uv run colab' to use the local development version
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s test-live-keep-alive

# Verify session exists in state
if [ ! -f "$SESSION_FILE" ]; then
    echo "Error: Session file '$SESSION_FILE' not created."
    exit 1
fi

grep "test-live-keep-alive" "$SESSION_FILE"

# Extract PID
PID=$(grep -A 15 "test-live-keep-alive" "$SESSION_FILE" | grep "keep_alive_pid" | awk '{print $2}' | tr -d ',')

if [ -z "$PID" ] || [ "$PID" == "null" ]; then
    echo "Error: No keep_alive_pid found in state file."
    cat "$SESSION_FILE"
    exit 1
fi

echo "Keep-alive PID: $PID"

# Verify process is running
if ps -p $PID > /dev/null; then
   echo "Keep-alive process is running."
else
   echo "Error: Keep-alive process NOT running."
   exit 1
fi

# Verify the process command line is actually colab keep-alive
ps -fp $PID | grep "keep-alive"

echo "Stopping session (REAL API CALL)..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s test-live-keep-alive

# Give it a tiny bit of time to terminate
sleep 1

if ! ps -p $PID > /dev/null; then
   echo "Keep-alive process terminated successfully."
else
   echo "Error: Keep-alive process still running after stop!"
   kill $PID
   exit 1
fi

echo "Live Integration test passed!"
