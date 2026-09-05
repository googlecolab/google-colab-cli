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

"""Windows-only console helpers implemented with stdlib ctypes.

This module is imported only on ``sys.platform == "win32"`` and provides the
raw console mode handling that POSIX builds get from ``termios``/``tty``.
"""

import contextlib
import ctypes
from ctypes import wintypes

kernel32 = ctypes.windll.kernel32

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

ENABLE_ECHO_INPUT = 0x0004
ENABLE_LINE_INPUT = 0x0002
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80


class _COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.GetStdHandle.restype = wintypes.HANDLE

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
kernel32.GetConsoleMode.restype = wintypes.BOOL

kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.SetConsoleMode.restype = wintypes.BOOL

kernel32.GetConsoleScreenBufferInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO),
]
kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL


def _win_error() -> OSError:
    """Return an OSError for the current Windows last-error code."""
    return ctypes.WinError(ctypes.get_last_error())


def get_std_handle(handle_id: int) -> wintypes.HANDLE:
    """Return a Windows standard console handle."""
    handle = kernel32.GetStdHandle(handle_id)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise _win_error()
    return handle


def open_console_device(name: str, access: int) -> wintypes.HANDLE:
    """Open a console device (``CONIN$`` or ``CONOUT$``) and return its handle.

    Unlike ``GetStdHandle``, this works even when the standard streams are
    redirected because it opens the console directly.
    """
    handle = kernel32.CreateFileW(
        name,
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise _win_error()
    return handle


def close_handle(handle: wintypes.HANDLE) -> None:
    """Close a handle opened with :func:`open_console_device`."""
    kernel32.CloseHandle(handle)


def get_console_mode(handle: wintypes.HANDLE) -> int:
    """Return the current console mode for *handle*."""
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        raise _win_error()
    return mode.value


def set_console_mode(handle: wintypes.HANDLE, mode: int) -> None:
    """Set the console mode for *handle* to *mode*."""
    if not kernel32.SetConsoleMode(handle, mode):
        raise _win_error()


def get_console_size():
    """Return ``(columns, rows)`` for the active console, or ``None``.

    Falls back to ``None`` when the process has no console, which lets callers
    keep polling without aborting the session.
    """
    try:
        handle = open_console_device(
            "CONOUT$", GENERIC_READ | GENERIC_WRITE
        )
    except OSError:
        return None

    try:
        info = _CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None

        width = info.srWindow.Right - info.srWindow.Left + 1
        height = info.srWindow.Bottom - info.srWindow.Top + 1
        return width, height
    finally:
        close_handle(handle)


@contextlib.contextmanager
def raw_mode():
    """Context manager that puts the console into raw mode.

    Saves the original input/output console modes, disables line buffering,
    echo and processed input, enables virtual-terminal input so that escape
    sequences from the remote shell are delivered as bytes, and enables VT
    processing on output so ANSI escapes render correctly. Original modes are
    restored on exit.

    Yields a ``(saved_input_mode, saved_output_mode)`` tuple.
    """
    in_handle = open_console_device("CONIN$", GENERIC_READ | GENERIC_WRITE)
    out_handle = open_console_device("CONOUT$", GENERIC_READ | GENERIC_WRITE)

    try:
        old_in_mode = get_console_mode(in_handle)
        old_out_mode = get_console_mode(out_handle)

        new_in_mode = old_in_mode
        new_in_mode &= ~ENABLE_ECHO_INPUT
        new_in_mode &= ~ENABLE_LINE_INPUT
        new_in_mode &= ~ENABLE_PROCESSED_INPUT
        new_in_mode |= ENABLE_VIRTUAL_TERMINAL_INPUT

        new_out_mode = old_out_mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING

        set_console_mode(in_handle, new_in_mode)
        set_console_mode(out_handle, new_out_mode)

        try:
            yield (old_in_mode, old_out_mode)
        finally:
            set_console_mode(in_handle, old_in_mode)
            set_console_mode(out_handle, old_out_mode)
    finally:
        close_handle(in_handle)
        close_handle(out_handle)
