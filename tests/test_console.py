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
import termios
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
    mock_setraw.assert_called_once_with(sys.stdin.fileno(), termios.TCSANOW)

    # Teardown should happen in a finally block
    mock_tcsetattr.assert_called_once_with(
        sys.stdin.fileno(), termios.TCSANOW, ["fake_attrs"]
    )


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.tty.setraw")
@patch("colab_cli.console.termios.tcgetattr")
@patch("colab_cli.console.termios.tcsetattr")
@patch("colab_cli.console.sys.stdin.isatty")
def test_console_piped_input(
    mock_isatty,
    mock_tcsetattr,
    mock_tcgetattr,
    mock_setraw,
    mock_ws_app,
    mock_session,
):
    mock_isatty.return_value = False
    mock_ws_instance = MagicMock()
    mock_ws_app.return_value = mock_ws_instance
    mock_ws_instance.run_forever.return_value = None

    with patch("colab_cli.console.threading.Thread"):
        connect_console(mock_session)

    # In a piped environment, we should not attempt to use termios or tty
    mock_tcgetattr.assert_not_called()
    mock_setraw.assert_not_called()
    mock_tcsetattr.assert_not_called()


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
