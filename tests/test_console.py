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
import os
import sys
from unittest.mock import MagicMock, patch

import colab_cli.console as console_mod
from colab_cli.console import connect_console, on_message, on_open
from colab_cli.state import SessionState
import pytest


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev",
        endpoint="some-endpoint",
    )


@pytest.mark.skipif(
    not console_mod.HAS_TERMIOS, reason="Unix raw TTY setup uses termios/tty"
)
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.fileno")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_initialization(
    mock_isatty,
    mock_fileno,
    mock_get_term_size,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    # Setup mocks
    mock_isatty.return_value = True
    mock_fileno.return_value = 0
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_tcgetattr.return_value = ["fake_attrs"]
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance

    # We don't want run_forever to actually block or start threads in the test
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # 1. Verify URL transformation
    expected_url = "wss://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev/colab/tty?colab-runtime-proxy-token=test-token"
    mock_ws_app.assert_called_once()
    assert mock_ws_app.call_args[1]["url"] == expected_url

    # 2. Verify raw mode setup and teardown
    mock_tcgetattr.assert_called_once_with(sys.stdin.fileno())
    mock_setraw.assert_called_once_with(sys.stdin.fileno(), console_mod.termios.TCSANOW)

    # Teardown should happen in a finally block
    mock_tcsetattr.assert_called_once_with(
        sys.stdin.fileno(), console_mod.termios.TCSANOW, ["fake_attrs"]
    )


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_piped_input(
    mock_isatty,
    mock_ws_app,
    mock_session,
):
    mock_isatty.return_value = False
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    assert mock_ws_instance.run_forever.called


@patch("colab_cli.console.threading.Thread")
@patch("colab_cli.console.os.get_terminal_size")
def test_on_open_sends_terminal_size(mock_get_term_size, mock_thread):
    mock_ws = MagicMock()
    mock_get_term_size.return_value = os.terminal_size((100, 40))

    on_open(mock_ws)

    # Verify that the initial terminal size is sent
    mock_ws.send.assert_called_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload == {"cols": 100, "rows": 40}
    mock_thread.return_value.start.assert_called_once()


@patch("colab_cli.console.sys.stdout.buffer.write")
@patch("colab_cli.console.sys.stdout.buffer.flush")
def test_on_message_writes_to_stdout(mock_flush, mock_write):
    mock_ws = MagicMock()
    test_data = "Hello \x1b[34mWorld\x1b[0m"
    message_json = json.dumps({"data": test_data})

    on_message(mock_ws, message_json)

    # Verify that the data is written exactly as received
    mock_write.assert_called_once_with(test_data.encode("utf-8"))
    mock_flush.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_piped_sends_exit_and_closes_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is piped and reaches EOF, the read thread should send 'exit\\n'
    to the remote shell and then close the websocket from the client side.

    The remote shell at /colab/tty is wrapped in tmux which swallows the bare
    \\x04 (Ctrl-D) we used to send, so EOF used to leave the websocket open
    indefinitely. Sending 'exit\\n' + ws.close() guarantees clean termination.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = False
    # Simulate piped stdin: returns one line then EOF
    mock_stdin.read.side_effect = ["e", "c", "h", "o", " ", "h", "i", "\n", ""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    # on_open spawns the read thread; we want it to run synchronously here
    # so we patch threading.Thread to call target immediately and join().
    real_thread = []

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target
            real_thread.append(self)

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread):
        # Use a tiny grace period for the test
        with patch("colab_cli.console.PIPED_EOF_GRACE_SECONDS", 0.01):
            on_open(mock_ws)

    # Collect what was sent to the websocket
    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]

    # Initial send is the terminal size; everything after is stdin chars or our exit string.
    # Verify "exit\n" was sent on EOF (one send per character)
    assert {"data": "exit\n"} in sent_payloads, (
        f"Expected 'exit\\n' to be sent on piped EOF, got: {sent_payloads}"
    )

    # Verify we closed the websocket from the client side
    mock_ws.close.assert_called_once()


@pytest.mark.skipif(
    not console_mod.HAS_TERMIOS, reason="TTY EOF behavior is Unix termios-specific"
)
@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_tty_does_not_close_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    """When stdin is a real TTY and read() returns empty (which happens on
    Ctrl-D in raw mode), we should NOT inject 'exit\\n' or close the websocket
    \u2014 the user is in interactive mode and may have intended Ctrl-D as a literal
    char. The websocket lifecycle is owned by the remote shell in this case.
    """
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    # TTY EOF is rare but possible; should be passed through transparently
    mock_stdin.read.side_effect = [""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread):
        on_open(mock_ws)

    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
    assert {"data": "exit\n"} not in sent_payloads
    mock_ws.close.assert_not_called()


@pytest.mark.skipif(
    console_mod.HAS_TERMIOS, reason="Windows console mode is Windows-specific"
)
def test_windows_console_mode_reports_get_console_mode_failure(monkeypatch):
    class FailingKernel32:
        def GetStdHandle(self, handle_id):
            return 10 if handle_id == console_mod.STD_INPUT_HANDLE else 11

        def GetConsoleMode(self, handle, mode_ptr):
            return 0

        def SetConsoleMode(self, handle, mode):
            raise AssertionError("SetConsoleMode should not be called")

    monkeypatch.setattr(console_mod, "_KERNEL32", FailingKernel32())
    monkeypatch.setattr(console_mod.ctypes, "get_last_error", lambda: 6)

    monkeypatch.setattr(console_mod.ctypes, "FormatError", lambda code: "bad handle")

    with pytest.raises(OSError) as exc_info:
        with console_mod.WindowsConsoleMode():
            pass

    message = str(exc_info.value)
    assert "Windows console support is unavailable" in message
    assert "GetConsoleMode(stdin) failed: bad handle (WinError 6)" in message
    assert "terminal is detached or lacks console support" in message
    assert "QUICK_START_WINDOWS.md" in message
    assert "3.11+" in message


@pytest.mark.skipif(
    console_mod.HAS_TERMIOS, reason="Windows console mode is Windows-specific"
)
def test_windows_console_mode_restores_after_keyboard_interrupt(monkeypatch):
    calls = []

    class Kernel32:
        def GetStdHandle(self, handle_id):
            return 10 if handle_id == console_mod.STD_INPUT_HANDLE else 11

        def GetConsoleMode(self, handle, mode_ptr):
            mode_ptr._obj.value = 0x0007 if handle == 10 else 0x0001
            return 1

        def SetConsoleMode(self, handle, mode):
            calls.append((handle, mode))
            return 1

    monkeypatch.setattr(console_mod, "_KERNEL32", Kernel32())

    with pytest.raises(KeyboardInterrupt):
        with console_mod.WindowsConsoleMode():
            raise KeyboardInterrupt

    assert calls[-2:] == [(10, 0x0007), (11, 0x0001)]


@pytest.mark.skipif(
    console_mod.HAS_TERMIOS, reason="Windows console mode is Windows-specific"
)
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.websocket.WebSocketApp")
def test_connect_console_restores_windows_mode_after_keyboard_interrupt(
    mock_ws_app, mock_isatty, monkeypatch, mock_session
):
    calls = []

    class Kernel32:
        def GetStdHandle(self, handle_id):
            return 10 if handle_id == console_mod.STD_INPUT_HANDLE else 11

        def GetConsoleMode(self, handle, mode_ptr):
            mode_ptr._obj.value = 0x0007 if handle == 10 else 0x0001
            return 1

        def SetConsoleMode(self, handle, mode):
            calls.append((handle, mode))
            return 1

    mock_isatty.return_value = True
    mock_ws = MagicMock()
    mock_ws.run_forever.side_effect = KeyboardInterrupt
    mock_ws_app.return_value = mock_ws
    monkeypatch.setattr(console_mod, "_KERNEL32", Kernel32())

    with pytest.raises(KeyboardInterrupt):
        connect_console(mock_session)

    assert calls[-2:] == [(10, 0x0007), (11, 0x0001)]
    mock_ws.close.assert_called_once()


@pytest.mark.skipif(
    console_mod.HAS_TERMIOS, reason="Windows console mode is Windows-specific"
)
def test_windows_console_mode_logs_restore_failure_during_exception(
    monkeypatch, caplog
):
    calls = []

    class Kernel32:
        def GetStdHandle(self, handle_id):
            return 10 if handle_id == console_mod.STD_INPUT_HANDLE else 11

        def GetConsoleMode(self, handle, mode_ptr):
            mode_ptr._obj.value = 0x0007 if handle == 10 else 0x0001
            return 1

        def SetConsoleMode(self, handle, mode):
            calls.append((handle, mode))
            return 0 if len(calls) >= 3 else 1

    monkeypatch.setattr(console_mod, "_KERNEL32", Kernel32())
    monkeypatch.setattr(console_mod.ctypes, "get_last_error", lambda: 5)
    monkeypatch.setattr(console_mod.ctypes, "FormatError", lambda code: "access denied")

    with caplog.at_level("ERROR"):
        with pytest.raises(KeyboardInterrupt):
            with console_mod.WindowsConsoleMode():
                raise KeyboardInterrupt

    assert "Failed to restore Windows console mode" in caplog.text
    assert "KeyboardInterrupt" in caplog.text


@pytest.mark.skipif(
    console_mod.HAS_TERMIOS, reason="Windows console input is Windows-specific"
)
def test_read_char_windows_does_not_block_when_no_input(monkeypatch):
    getwch = MagicMock()
    monkeypatch.setattr(console_mod.msvcrt, "kbhit", lambda: False)
    monkeypatch.setattr(console_mod.msvcrt, "getwch", getwch)

    assert console_mod.read_char_windows() is None
    getwch.assert_not_called()


@pytest.mark.skipif(
    console_mod.HAS_TERMIOS, reason="Windows console input is Windows-specific"
)
def test_read_char_windows_does_not_block_on_extended_key_prefix(monkeypatch):
    getwch = MagicMock(return_value="\xe0")
    kbhit = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(console_mod.msvcrt, "kbhit", kbhit)
    monkeypatch.setattr(console_mod.msvcrt, "getwch", getwch)

    assert console_mod.read_char_windows() is None
    assert getwch.call_count == 1
