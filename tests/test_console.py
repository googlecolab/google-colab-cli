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


posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX termios/tty not available on Windows"
)
win32_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only test"
)


@posix_only
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
    import colab_cli.console as console_mod

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
    mock_setraw.assert_called_once_with(
        sys.stdin.fileno(), console_mod.termios.TCSANOW
    )

    # Teardown should happen in a finally block
    mock_tcsetattr.assert_called_once_with(
        sys.stdin.fileno(), console_mod.termios.TCSANOW, ["fake_attrs"]
    )


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_piped_input(
    mock_isatty, mock_ws_app, mock_session
):
    mock_isatty.return_value = False
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # In a piped environment, we should not attempt to use termios or tty
    mock_ws_app.assert_called_once()
    mock_ws_instance.run_forever.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
def test_on_open_sends_terminal_size(mock_get_term_size):
    mock_ws = MagicMock()
    mock_get_term_size.return_value = os.terminal_size((100, 40))

    on_open(mock_ws)

    # Verify that the initial terminal size is sent
    mock_ws.send.assert_called_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload == {"cols": 100, "rows": 40}


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


@win32_only
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console._winconsole.raw_mode")
@patch("colab_cli.console._winconsole.get_console_size")
def test_console_initialization_windows(
    mock_get_console_size,
    mock_raw_mode,
    mock_isatty,
    mock_ws_app,
    mock_session,
):
    """Windows TTY branch uses ctypes raw mode and a resize poller thread."""
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    mock_get_console_size.return_value = (80, 24)
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=(0, 0))
    mock_cm.__exit__ = MagicMock(return_value=False)
    mock_raw_mode.return_value = mock_cm

    with patch("colab_cli.console.threading.Thread") as mock_thread:
        connect_console(mock_session)

    # 1. Verify URL transformation
    expected_url = "wss://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev/colab/tty?colab-runtime-proxy-token=test-token"
    mock_ws_app.assert_called_once()
    assert mock_ws_app.call_args[1]["url"] == expected_url

    # 2. Verify the Windows raw-mode context manager is entered/exited
    mock_raw_mode.assert_called_once()
    mock_cm.__enter__.assert_called_once()
    mock_cm.__exit__.assert_called_once()

    # 3. Resize poller thread is spawned
    mock_thread.assert_called_once()
    assert mock_thread.call_args[1].get("daemon") is True

    # 4. _is_running is cleared after the connection ends
    assert console_mod._is_running is False


@win32_only
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console._winconsole.raw_mode")
def test_console_windows_tty_restores_modes_on_exception(
    mock_raw_mode, mock_isatty, mock_ws_app, mock_session
):
    """Even if ws.run_forever() raises, the Windows raw-mode context exits."""
    mock_isatty.return_value = True
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.side_effect = RuntimeError("boom")

    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=(0, 0))
    mock_cm.__exit__ = MagicMock(return_value=False)
    mock_raw_mode.return_value = mock_cm

    with patch("colab_cli.console.threading.Thread"):
        with pytest.raises(RuntimeError, match="boom"):
            connect_console(mock_session)

    mock_cm.__exit__.assert_called_once()


@win32_only
def test_winconsole_raw_mode_sets_and_restores_real_modes():
    """Exercise the real Windows console mode save/set/restore helper.

    This calls the actual kernel32 console APIs against the process's
    CONIN$/CONOUT$ handles and verifies the expected raw-mode flags.
    """
    import colab_cli._winconsole as wc

    try:
        in_handle = wc.open_console_device(
            "CONIN$", wc.GENERIC_READ | wc.GENERIC_WRITE
        )
        out_handle = wc.open_console_device(
            "CONOUT$", wc.GENERIC_READ | wc.GENERIC_WRITE
        )
    except OSError as exc:
        pytest.skip(f"No real console available in this test environment: {exc}")

    try:
        original_in_mode = wc.get_console_mode(in_handle)
        original_out_mode = wc.get_console_mode(out_handle)

        with wc.raw_mode():
            new_in_mode = wc.get_console_mode(in_handle)
            assert (new_in_mode & wc.ENABLE_VIRTUAL_TERMINAL_INPUT) != 0
            assert (new_in_mode & wc.ENABLE_LINE_INPUT) == 0
            assert (new_in_mode & wc.ENABLE_ECHO_INPUT) == 0
            assert (new_in_mode & wc.ENABLE_PROCESSED_INPUT) == 0

            new_out_mode = wc.get_console_mode(out_handle)
            assert (new_out_mode & wc.ENABLE_VIRTUAL_TERMINAL_PROCESSING) != 0

        restored_in_mode = wc.get_console_mode(in_handle)
        restored_out_mode = wc.get_console_mode(out_handle)
    finally:
        wc.close_handle(in_handle)
        wc.close_handle(out_handle)

    assert restored_in_mode == original_in_mode
    assert restored_out_mode == original_out_mode


@win32_only
def test_winconsole_get_console_size_returns_dimensions():
    """get_console_size should return positive (width, height) on a real console."""
    import colab_cli._winconsole as wc

    size = wc.get_console_size()
    if size is None:
        pytest.skip("No real console available in this test environment")
    width, height = size
    assert width > 0
    assert height > 0
