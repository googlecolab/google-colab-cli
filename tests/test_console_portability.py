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

"""Keep upstream terminal fallbacks when using the reconnecting transport."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from colab_cli import console
from colab_cli.state import SessionState


@pytest.mark.parametrize(
    "failure", ["no-termios", "tcgetattr", "no-sigwinch", "restore"]
)
def test_console_terminal_fallbacks(monkeypatch, failure):
    terminal = MagicMock()
    tty = MagicMock()
    signals = SimpleNamespace(signal=MagicMock(), getsignal=MagicMock(), SIGWINCH=28)
    if failure == "no-termios":
        terminal = tty = None
    elif failure == "tcgetattr":
        terminal.tcgetattr.side_effect = OSError("no terminal attributes")
    elif failure == "no-sigwinch":
        del signals.SIGWINCH
    elif failure == "restore":
        terminal.tcsetattr.side_effect = OSError("terminal already closed")
    monkeypatch.setattr(console, "termios", terminal)
    monkeypatch.setattr(console, "tty", tty)
    monkeypatch.setattr(console, "signal", signals)
    monkeypatch.setattr(
        console.sys, "stdin", MagicMock(encoding="utf-8", errors="strict")
    )
    ws = MagicMock()

    def make_ws(**callbacks):
        ws.run_forever.side_effect = lambda **kwargs: callbacks["on_close"](
            ws, 1000, "done"
        )
        return ws

    monkeypatch.setattr(console.websocket, "WebSocketApp", make_ws)
    console.connect_console(
        SessionState(name="s1", endpoint="ep1", token="token", url="https://vm")
    )
    ws.run_forever.assert_called_once()
    if terminal is None or failure == "tcgetattr":
        signals.signal.assert_not_called()


def test_console_without_posix_terminal_uses_text_input(monkeypatch):
    monkeypatch.setattr(console, "termios", None)
    monkeypatch.setattr(
        console.sys, "stdin", MagicMock(encoding="utf-8", errors="strict")
    )
    monkeypatch.setattr(
        console.select, "select", MagicMock(side_effect=OSError("not selectable"))
    )
    forwarder = console._ConsoleInputForwarder(is_tty=True)
    forwarder._read_text_stdin = MagicMock()
    forwarder._read_stdin()
    forwarder._read_text_stdin.assert_called_once()
