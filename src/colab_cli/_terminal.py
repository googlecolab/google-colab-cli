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

"""Platform abstraction for terminal raw-mode handling.

Provides a uniform API for putting a terminal into raw (character-at-a-time,
no-echo) mode and restoring it afterwards.  On Linux/macOS it delegates to
``termios`` + ``tty``; on Windows it uses the Console API via ``ctypes``.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_fd() -> int | None:
    """Return the file descriptor for stdin if it is a TTY, else None."""
    if not os.isatty(0):
        return None
    return _get_fd()


def set_raw(fd: int):
    """Put the terminal referenced by *fd* into raw mode.

    Returns an opaque *old_state* object that must be passed to
    :func:`restore` when raw mode is no longer needed.
    """
    return _set_raw(fd)


def restore(fd: int, old_state) -> None:
    """Restore the terminal to the settings captured by :func:`set_raw`."""
    _restore(fd, old_state)


def register_resize_handler(callback) -> None:
    """Register *callback* to be invoked when the terminal window is resized.

    The callback receives no arguments and should read the new size via
    :func:`os.get_terminal_size`.
    """
    _register_resize_handler(callback)


def unregister_resize_handler() -> None:
    """Remove any resize handler registered by :func:`register_resize_handler`."""
    _unregister_resize_handler()


# ---------------------------------------------------------------------------
# Windows implementation (ctypes + msvcrt)
# ---------------------------------------------------------------------------

if os.name == "nt":
    import msvcrt
    from ctypes import c_ulong, byref, windll, WINFUNCTYPE

    kernel32 = windll.kernel32

    # Console mode flags
    _ENABLE_PROCESSED_INPUT = 0x0001
    _ENABLE_LINE_INPUT = 0x0002
    _ENABLE_ECHO_INPUT = 0x0004
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

    _resize_thread = None
    _resize_stop = None

    def _get_handle(fd: int):
        return msvcrt.get_osfhandle(fd)

    def _get_fd() -> int | None:
        return 0  # stdin

    def _set_raw(fd: int):
        handle = _get_handle(fd)
        mode = c_ulong()
        kernel32.GetConsoleMode(handle, byref(mode))
        old_mode = mode.value

        # Disable processed input (Ctrl+C handling), line input, and echo.
        # Enable virtual terminal input so ANSI escape sequences from the
        # remote TTY pass through.
        new_mode = (
            old_mode
            & ~_ENABLE_PROCESSED_INPUT
            & ~_ENABLE_LINE_INPUT
            & ~_ENABLE_ECHO_INPUT
            | _ENABLE_VIRTUAL_TERMINAL_INPUT
        )
        kernel32.SetConsoleMode(handle, new_mode)
        return old_mode

    def _restore(fd: int, old_mode) -> None:
        handle = _get_handle(fd)
        kernel32.SetConsoleMode(handle, old_mode)

    def _resize_poll_loop(interval: float, callback):
        """Background thread that polls terminal size and calls *callback* on change."""
        last = None
        while not _resize_stop.is_set():
            try:
                current = os.get_terminal_size()
                if last is not None and current != last:
                    try:
                        callback()
                    except Exception:
                        logger.debug("Resize callback failed", exc_info=True)
                last = current
            except Exception:
                pass
            _resize_stop.wait(interval)

    def _register_resize_handler(callback) -> None:
        global _resize_thread, _resize_stop
        _unregister_resize_handler()
        _resize_stop = threading.Event()
        _resize_thread = threading.Thread(
            target=_resize_poll_loop,
            args=(0.5, callback),
            daemon=True,
        )
        _resize_thread.start()

    def _unregister_resize_handler() -> None:
        global _resize_thread, _resize_stop
        if _resize_stop is not None:
            _resize_stop.set()
        if _resize_thread is not None:
            _resize_thread.join(timeout=1.0)
            _resize_thread = None
        _resize_stop = None

# ---------------------------------------------------------------------------
# Unix implementation (termios + tty)
# ---------------------------------------------------------------------------

else:
    import signal
    import termios
    import tty

    def _get_fd() -> int | None:
        return 0  # stdin

    def _set_raw(fd: int):
        old = termios.tcgetattr(fd)
        tty.setraw(fd, termios.TCSANOW)
        return old

    def _restore(fd: int, old) -> None:
        termios.tcsetattr(fd, termios.TCSANOW, old)

    def _register_resize_handler(callback) -> None:
        # Wrap so we swallow the signum/frame arguments the callback doesn't need.
        def handler(signum, frame):
            callback()

        signal.signal(signal.SIGWINCH, handler)

    def _unregister_resize_handler() -> None:
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
