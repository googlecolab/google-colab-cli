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
import threading
import time
from urllib.parse import urlparse

# Windows compatibility: termios/tty are Unix-only
try:
    import termios
    import tty
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False
    # Windows-specific imports
    import msvcrt
    import ctypes
    from ctypes import wintypes

import websocket

from colab_cli.state import SessionState

logger = logging.getLogger(__name__)

# Global flag to stop the read thread when the websocket closes
_is_running = False
_running_event = threading.Event()
_last_error = None

# When stdin is piped and reaches EOF, we send "exit\n" to the remote shell and
# then wait this many seconds for any remaining output (the shell's goodbye,
# tmux teardown messages, etc.) to flush before closing the websocket from the
# client side. Empirically 0.5s is enough for the typical /colab/tty backend
# wrapped in tmux + bash; bumping it just delays exit, lowering it risks
# truncating tail output.
PIPED_EOF_GRACE_SECONDS = 0.5
WINDOWS_MIN_PYTHON = (3, 11)
WINDOWS_QUICK_START = "QUICK_START_WINDOWS.md"


# Windows console mode constants
if not HAS_TERMIOS:
    ENABLE_ECHO_INPUT = 0x0004
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_fallback_message(reason: str) -> str:
    min_version = ".".join(str(part) for part in WINDOWS_MIN_PYTHON)
    return (
        f"Windows console support is unavailable: {reason}. "
        f"See {WINDOWS_QUICK_START}. "
        f"Minimum Python version tested/supported on Windows: {min_version}+."
    )


def _raise_windows_api_error(api_name: str) -> None:
    error_code = ctypes.get_last_error()
    error_message = ctypes.FormatError(error_code).strip()
    raise OSError(
        error_code,
        _windows_fallback_message(
            f"{api_name} failed: {error_message} (WinError {error_code}). "
            "This may indicate the terminal is detached or lacks console support."
        ),
    )


def _set_console_running(value: bool) -> None:
    global _is_running
    _is_running = value
    if value:
        _running_event.set()
    else:
        _running_event.clear()


def _console_running() -> bool:
    return _running_event.is_set()


def _get_console_mode(handle, stream_name: str) -> int:
    mode = wintypes.DWORD()
    if not _KERNEL32.GetConsoleMode(handle, ctypes.byref(mode)):
        _raise_windows_api_error(f"GetConsoleMode({stream_name})")
    return mode.value


def _set_console_mode(handle, mode: int, stream_name: str) -> None:
    if not _KERNEL32.SetConsoleMode(handle, mode):
        _raise_windows_api_error(f"SetConsoleMode({stream_name})")


class WindowsConsoleMode:
    """Context manager for Windows console raw mode."""

    def __init__(self):
        if HAS_TERMIOS:
            return

        if sys.version_info < WINDOWS_MIN_PYTHON:
            version = ".".join(str(part) for part in sys.version_info[:3])
            raise RuntimeError(
                _windows_fallback_message(
                    f"Python {version} is older than the supported version"
                )
            )

        self.stdin_handle = _KERNEL32.GetStdHandle(STD_INPUT_HANDLE)
        self.stdout_handle = _KERNEL32.GetStdHandle(STD_OUTPUT_HANDLE)
        if self.stdin_handle in (0, INVALID_HANDLE_VALUE):
            raise RuntimeError(_windows_fallback_message("stdin is not a console"))
        if self.stdout_handle in (0, INVALID_HANDLE_VALUE):
            raise RuntimeError(_windows_fallback_message("stdout is not a console"))

        self.old_stdin_mode = None
        self.old_stdout_mode = None
        self._entered = False

    def __enter__(self):
        if HAS_TERMIOS:
            return self

        # Get current modes
        self.old_stdin_mode = _get_console_mode(self.stdin_handle, "stdin")
        self.old_stdout_mode = _get_console_mode(self.stdout_handle, "stdout")

        # Set raw mode for stdin (disable line input and echo)
        new_stdin_mode = self.old_stdin_mode & ~(
            ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT
        )
        _set_console_mode(self.stdin_handle, new_stdin_mode, "stdin")

        # Enable ANSI/VT processing for stdout
        new_stdout_mode = self.old_stdout_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        try:
            _set_console_mode(self.stdout_handle, new_stdout_mode, "stdout")
        except Exception:
            _set_console_mode(self.stdin_handle, self.old_stdin_mode, "stdin")
            raise

        self._entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if HAS_TERMIOS:
            return

        # Restore original modes
        restore_errors = []
        if self.old_stdin_mode is not None:
            try:
                _set_console_mode(self.stdin_handle, self.old_stdin_mode, "stdin")
            except Exception as e:
                restore_errors.append(e)
        if self.old_stdout_mode is not None:
            try:
                _set_console_mode(self.stdout_handle, self.old_stdout_mode, "stdout")
            except Exception as e:
                restore_errors.append(e)
        self._entered = False
        if restore_errors and exc_type is None:
            raise restore_errors[0]
        if restore_errors:
            logger.error(
                "Failed to restore Windows console mode while handling %s: %s",
                exc_type.__name__,
                restore_errors,
            )


def read_char_windows():
    """Read a single character on Windows using msvcrt."""
    if msvcrt.kbhit():
        char = msvcrt.getwch()
        # Handle special keys (arrows, function keys, etc.)
        if char in ('\x00', '\xe0'):
            # Extended key, read the second byte
            if msvcrt.kbhit():
                msvcrt.getwch()  # Consume but ignore for now
            return None
        return char
    return None


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
    _set_console_running(False)


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
    _set_console_running(True)

    # Send initial terminal size
    send_terminal_size(ws)

    # Setup the background thread to read from stdin
    def read_stdin():
        is_tty = sys.stdin.isatty()

        if not is_tty:
            # Non-TTY mode (piped input)
            while _console_running():
                try:
                    char = sys.stdin.read(1)
                    if not char:
                        # EOF reached
                        try:
                            ws.send(json.dumps({"data": "exit\n"}))
                        except Exception:
                            pass
                        time.sleep(PIPED_EOF_GRACE_SECONDS)
                        try:
                            ws.close()
                        except Exception:
                            pass
                        break
                    ws.send(json.dumps({"data": char}))
                except Exception:
                    break
        elif not HAS_TERMIOS:
            # Windows TTY mode - use msvcrt for character-by-character input
            while _console_running():
                try:
                    char = read_char_windows()
                    if char is not None:
                        ws.send(json.dumps({"data": char}))
                    else:
                        time.sleep(0.01)  # Small delay to prevent CPU spinning
                except KeyboardInterrupt:
                    _set_console_running(False)
                    try:
                        ws.close()
                    except Exception:
                        pass
                    break
                except Exception:
                    break
        else:
            # Unix TTY mode - read character by character
            while _console_running():
                try:
                    char = sys.stdin.read(1)
                    if not char:
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
    global _last_error
    _last_error = None

    # Construct the WebSocket URL from the base URL
    parsed = urlparse(session.url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{parsed.netloc}/colab/tty?colab-runtime-proxy-token={session.token}"

    is_tty = sys.stdin.isatty()
    
    # Platform-specific terminal setup
    if HAS_TERMIOS and is_tty:
        # Unix: use termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
    else:
        fd = None
        old_settings = None

    ws = websocket.WebSocketApp(
        url=ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def handle_sigwinch(signum, frame):
        """Handle window resize events."""
        if _console_running():
            send_terminal_size(ws)

    # Context manager for Windows console mode
    windows_console = None if HAS_TERMIOS else WindowsConsoleMode()

    try:
        if HAS_TERMIOS and is_tty:
            # Unix: set raw mode
            tty.setraw(fd, termios.TCSANOW)
            if hasattr(signal, 'SIGWINCH'):
                signal.signal(signal.SIGWINCH, handle_sigwinch)
        elif not HAS_TERMIOS and is_tty:
            # Windows: enter raw console mode
            windows_console.__enter__()

        # This is a blocking call until the connection is closed
        ws.run_forever(ping_interval=30, ping_timeout=10)

        if _last_error:
            # Re-raise or wrap terminal errors
            err_msg = str(_last_error)
            if "404" in err_msg or "401" in err_msg:
                raise RuntimeError(f"Connection failed: {err_msg}")
    finally:
        exc_type, exc_val, exc_tb = sys.exc_info()
        _set_console_running(False)
        try:
            ws.close()
        except Exception:
            pass
        if HAS_TERMIOS and is_tty:
            # Unix: restore terminal
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            if hasattr(signal, 'SIGWINCH'):
                signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        elif not HAS_TERMIOS and is_tty and windows_console:
            # Windows: restore console mode
            windows_console.__exit__(exc_type, exc_val, exc_tb)
        print("\r\nConnection closed.")
