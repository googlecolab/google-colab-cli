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

import json
import logging
import os
import signal
import sys
import termios
import threading
import tty
from urllib.parse import urlparse

import websocket

from colab_cli.state import SessionState

logger = logging.getLogger(__name__)

# Global flag to stop the read thread when the websocket closes
_is_running = False
_last_error = None


def on_message(ws, message):
    """Callback for when a message is received from the server."""
    try:
        data = json.loads(message)
        if "data" in data:
            # The backend sends raw ANSI escape sequences and string content.
            # We write it directly to stdout buffer to avoid python print() formatting.
            sys.stdout.buffer.write(data["data"].encode("utf-8"))
            sys.stdout.buffer.flush()
    except Exception as e:
        logger.debug(f"Error parsing message: {e}")


def on_error(ws, error):
    """Callback for when a websocket error occurs."""
    global _last_error
    _last_error = error
    logger.error(f"WebSocket Error: {error}")


def on_close(ws, close_status_code, close_msg):
    """Callback for when the websocket is closed."""
    global _is_running
    _is_running = False


def send_terminal_size(ws):
    """Sends the current terminal size to the remote backend."""
    try:
        size = os.get_terminal_size()
        payload = json.dumps({"cols": size.columns, "rows": size.lines})
        ws.send(payload)
    except Exception as e:
        logger.debug(f"Failed to send terminal size: {e}")


def on_open(ws):
    """Callback for when the websocket connection is opened."""
    global _is_running
    _is_running = True

    # Send initial terminal size
    send_terminal_size(ws)

    # Setup the background thread to read from stdin
    def read_stdin():
        is_tty = sys.stdin.isatty()
        while _is_running:
            try:
                # Read a single character (or escape sequence byte)
                char = sys.stdin.read(1)
                if not char:
                    if not is_tty:
                        # Piped input has reached EOF.
                        # Send a Ctrl+D to the remote terminal to exit bash gracefully.
                        ws.send(json.dumps({"data": "\x04"}))
                    break
                ws.send(json.dumps({"data": char}))
            except Exception:
                break

    thread = threading.Thread(target=read_stdin, daemon=True)
    thread.start()


def connect_console(session: SessionState):
    """
    Connects to the Colab TTY endpoint and sets up a raw terminal session.
    """
    global _is_running, _last_error
    _last_error = None

    # Construct the WebSocket URL from the base URL
    parsed = urlparse(session.url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/colab/tty?colab-runtime-proxy-token={session.token}"

    is_tty = sys.stdin.isatty()
    fd = sys.stdin.fileno() if is_tty else None
    old_settings = termios.tcgetattr(fd) if is_tty else None

    ws = websocket.WebSocketApp(
        url=ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def handle_sigwinch(signum, frame):
        """Handle window resize events."""
        if _is_running:
            send_terminal_size(ws)

    try:
        if is_tty:
            tty.setraw(fd, termios.TCSANOW)
            signal.signal(signal.SIGWINCH, handle_sigwinch)

        # This is a blocking call until the connection is closed
        ws.run_forever()

        if _last_error:
            # Re-raise or wrap terminal errors
            err_msg = str(_last_error)
            if "404" in err_msg or "401" in err_msg:
                # We raise a standard exception that the caller can recognize
                raise RuntimeError(f"Connection failed: {err_msg}")
    finally:
        if is_tty:
            # Always ensure the terminal is restored to its original state
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            # Restore the default signal handler for resize
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        print("\r\nConnection closed.")
