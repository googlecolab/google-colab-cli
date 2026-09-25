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

import pytest

from colab_cli.console import HAS_TERMIOS, connect_console, on_message, on_open
from colab_cli.state import SessionState


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev",
        endpoint="some-endpoint",
    )


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.fileno")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_initialization(
    mock_isatty,
    mock_fileno,
    mock_get_term_size,
    mock_ws_app,
    mock_session,
):
    mock_isatty.return_value = True
    mock_fileno.return_value = 0
    mock_get_term_size.return_value = os.terminal_size((80, 24))
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    if sys.platform == "win32":
        with patch("ctypes.windll.kernel32") as mock_kernel32:
            mock_kernel32.GetStdHandle.return_value = 1
            mock_kernel32.GetConsoleMode.return_value = True
            with patch("colab_cli.console.threading.Thread"):
                connect_console(mock_session)
            mock_kernel32.GetConsoleMode.assert_called()
    elif HAS_TERMIOS:
        with patch("colab_cli.console.tty.setraw") as mock_setraw, patch(
            "colab_cli.console.termios.tcgetattr"
        ) as mock_tcgetattr, patch(
            "colab_cli.console.termios.tcsetattr"
        ) as mock_tcsetattr:
            mock_tcgetattr.return_value = ["fake_attrs"]
            with patch("colab_cli.console.threading.Thread"):
                connect_console(mock_session)
            mock_tcgetattr.assert_called_once_with(sys.stdin.fileno())
            mock_setraw.assert_called_once_with(sys.stdin.fileno(), 0)
            mock_tcsetattr.assert_called_once_with(
                sys.stdin.fileno(), 0, ["fake_attrs"]
            )
    else:
        with patch("colab_cli.console.threading.Thread"):
            connect_console(mock_session)

    # Verify URL transformation
    expected_url = "wss://8080-m-s-kkb-usc1f1.us-central1-1.colab.dev/colab/tty?colab-runtime-proxy-token=test-token"
    mock_ws_app.assert_called_once()
    assert mock_ws_app.call_args[1]["url"] == expected_url


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

    # In a piped environment, we connect successfully without error
    mock_ws_app.assert_called_once()


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
    import colab_cli.console as console_mod

    mock_isatty.return_value = False
    # Simulate piped stdin: returns one line then EOF
    mock_stdin.read.side_effect = ["e", "c", "h", "o", " ", "h", "i", "\n", ""]
    mock_get_term_size.return_value = os.terminal_size((80, 24))

    mock_ws = MagicMock()

    class SyncThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    console_mod._is_running = True
    with patch("colab_cli.console.threading.Thread", SyncThread):
        with patch("colab_cli.console.PIPED_EOF_GRACE_SECONDS", 0.01):
            on_open(mock_ws)

    sent_payloads = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]

    assert {"data": "exit\n"} in sent_payloads, (
        f"Expected 'exit\\n' to be sent on piped EOF, got: {sent_payloads}"
    )

    mock_ws.close.assert_called_once()


@patch("colab_cli.console.os.get_terminal_size")
@patch("colab_cli.console.sys.stdin.isatty")
@patch("colab_cli.console.sys.stdin")
def test_read_stdin_eof_tty_does_not_close_ws(
    mock_stdin, mock_isatty, mock_get_term_size
):
    import colab_cli.console as console_mod

    mock_isatty.return_value = True
    if sys.platform == "win32":
        # On Windows, msvcrt.kbhit() is checked
        with patch("msvcrt.kbhit", side_effect=[False]):
            mock_get_term_size.return_value = os.terminal_size((80, 24))
            mock_ws = MagicMock()

            def stop_loop(*args, **kwargs):
                console_mod._is_running = False

            mock_ws.send.side_effect = stop_loop

            class SyncThread:
                def __init__(self, target, daemon=None):
                    self.target = target

                def start(self):
                    # Set running to false after one iteration to prevent infinite loop
                    console_mod._is_running = True

                    def run_once():
                        self.target()

                    run_once()

            with patch("colab_cli.console.threading.Thread", SyncThread):
                on_open(mock_ws)
            mock_ws.close.assert_not_called()
    else:
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
