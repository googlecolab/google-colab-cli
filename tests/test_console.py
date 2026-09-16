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

import base64
import gc
import hashlib
import json
import os
import socket
import sys
import termios
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, call, patch

import websocket

from colab_cli.console import (
    ConsoleConnectionError,
    _ConsoleInputForwarder,
    connect_console,
    on_message,
    on_open,
)
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

    # Long-lived interactive consoles must actively detect dead proxy links.
    mock_ws_instance.run_forever.assert_called_once_with(
        ping_interval=20, ping_timeout=10
    )

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


def _websocket_attempt(opened=True, error=None, close_code=1000, close_reason=""):
    """Returns a WebSocketApp mock that emits one deterministic lifecycle."""
    ws = MagicMock()

    def run_forever(**kwargs):
        if opened:
            ws._on_open(ws)
        if error is not None:
            ws._on_error(ws, error)
        ws._on_close(ws, close_code, close_reason)

    ws.run_forever.side_effect = run_forever
    return ws


def _connect_as_tty(*args, **kwargs):
    """Runs ``connect_console`` with terminal syscalls isolated from pytest."""
    with (
        patch("colab_cli.console.sys.stdin.fileno", return_value=0),
        patch("colab_cli.console.termios.tcgetattr", return_value=["attrs"]),
        patch("colab_cli.console.termios.tcsetattr"),
        patch("colab_cli.console.tty.setraw"),
        patch("colab_cli.console.signal.getsignal", return_value=MagicMock()),
        patch("colab_cli.console.signal.signal"),
    ):
        connect_console(*args, **kwargs)


@patch("colab_cli.console.threading.Thread")
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_reconnects_with_fresh_credentials_and_visible_status(
    _mock_isatty, mock_ws_app, mock_thread, mock_session, capsys
):
    """An abnormal post-connect close refreshes credentials and reconnects."""
    first = _websocket_attempt(
        error=websocket.WebSocketConnectionClosedException("proxy link lost"),
        close_code=1006,
        close_reason="abnormal closure",
    )
    second = _websocket_attempt(close_code=1000, close_reason="shell exited")
    attempts = iter([first, second])

    def make_ws(**kwargs):
        ws = next(attempts)
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws
    refreshed = SessionState(
        name=mock_session.name,
        token="fresh-token",
        url="https://fresh-runtime.example.test",
        endpoint=mock_session.endpoint,
    )
    refresh = MagicMock(return_value=refreshed)

    _connect_as_tty(
        mock_session,
        refresh_session=refresh,
        retry_delays=(0,),
        _max_reconnect_attempts=1,
    )

    refresh.assert_called_once_with(mock_session)
    assert mock_ws_app.call_count == 2
    assert "fresh-token" in mock_ws_app.call_args.kwargs["url"]
    # Reconnects reuse the one stdin reader instead of racing for terminal input.
    assert mock_thread.call_count == 1
    stderr = capsys.readouterr().err
    assert "Console connection lost" in stderr
    assert "Reconnecting in 0s (attempt 1" in stderr
    assert "Console reconnected" in stderr


@patch("colab_cli.console.threading.Thread")
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_stops_reconnecting_when_endpoint_is_confirmed_gone(
    _mock_isatty, mock_ws_app, _mock_thread, mock_session, capsys
):
    ws = _websocket_attempt(
        error=websocket.WebSocketConnectionClosedException("proxy link lost"),
        close_code=1006,
    )

    def make_ws(**kwargs):
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws
    refresh = MagicMock(return_value=None)

    _connect_as_tty(
        mock_session,
        refresh_session=refresh,
        retry_delays=(0,),
        _max_reconnect_attempts=1,
    )

    assert mock_ws_app.call_count == 1
    assert "no longer active" in capsys.readouterr().err


@patch("colab_cli.console.threading.Thread")
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_transient_refresh_failure_preserves_binding_and_retries(
    _mock_isatty, mock_ws_app, _mock_thread, mock_session, capsys
):
    first = _websocket_attempt(
        error=websocket.WebSocketConnectionClosedException("proxy link lost"),
        close_code=1006,
    )
    second = _websocket_attempt(close_code=1000)
    attempts = iter([first, second])

    def make_ws(**kwargs):
        ws = next(attempts)
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws
    refresh = MagicMock(side_effect=OSError("control plane unavailable"))

    _connect_as_tty(
        mock_session,
        refresh_session=refresh,
        retry_delays=(0,),
        _max_reconnect_attempts=1,
    )

    assert mock_ws_app.call_count == 2
    assert "could not refresh credentials" in capsys.readouterr().err


@patch("colab_cli.console.threading.Thread")
@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_refuses_same_name_replacement_endpoint(
    _mock_isatty, mock_ws_app, _mock_thread, mock_session, capsys
):
    ws = _websocket_attempt(
        error=websocket.WebSocketConnectionClosedException("proxy link lost"),
        close_code=1006,
    )

    def make_ws(**kwargs):
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws
    replacement = SessionState(
        name=mock_session.name,
        token="replacement-token",
        url="https://replacement.example.test",
        endpoint="replacement-endpoint",
    )

    _connect_as_tty(
        mock_session,
        refresh_session=MagicMock(return_value=replacement),
        retry_delays=(0,),
        _max_reconnect_attempts=1,
    )

    assert mock_ws_app.call_count == 1
    assert "different runtime" in capsys.readouterr().err


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_user_exit_does_not_reconnect(_mock_isatty, mock_ws_app, mock_session):
    ws = _websocket_attempt(
        error=websocket.WebSocketProtocolException("empty close frame"),
        close_code=None,
    )

    forwarder = MagicMock()
    forwarder.stop_event.is_set.return_value = False
    forwarder.user_requested_close = False

    def make_ws(**kwargs):
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        original_open = ws._on_open

        def opened(current_ws):
            original_open(current_ws)
            # Models the stdin worker recognizing `exit\n` before peer close.
            forwarder.user_requested_close = True

        ws._on_open = opened
        return ws

    mock_ws_app.side_effect = make_ws
    with patch("colab_cli.console._ConsoleInputForwarder", return_value=forwarder):
        _connect_as_tty(mock_session, retry_delays=(0,), _max_reconnect_attempts=1)

    assert mock_ws_app.call_count == 1


@pytest.mark.parametrize("command", ["exit\n", "logout\n", "\x04"])
def test_console_shell_exit_intent_expires(command):
    """Nested-shell exit input must not disable reconnect for the process lifetime."""
    forwarder = _ConsoleInputForwarder(is_tty=True)

    with patch("colab_cli.console.time.monotonic", return_value=10.0):
        for char in command:
            forwarder._track_exit_request(char)

    with patch("colab_cli.console.time.monotonic", return_value=11.9):
        assert forwarder.user_requested_close is True
    with patch("colab_cli.console.time.monotonic", return_value=12.1):
        assert forwarder.user_requested_close is False


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_stable_connection_resets_retry_backoff(
    _mock_isatty, mock_ws_app, mock_session, capsys
):
    """A later independent outage starts a fresh visible retry sequence."""
    attempts = iter(
        [
            _websocket_attempt(close_code=1006, close_reason="first loss"),
            _websocket_attempt(close_code=1006, close_reason="short recovery"),
            _websocket_attempt(close_code=1006, close_reason="later loss"),
            _websocket_attempt(close_code=1000, close_reason="shell exited"),
        ]
    )

    def make_ws(**kwargs):
        ws = next(attempts)
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws
    forwarder = MagicMock()
    forwarder.stop_event.is_set.return_value = False
    forwarder.stop_event.wait.return_value = False
    forwarder.user_requested_close = False

    with (
        patch("colab_cli.console._ConsoleInputForwarder", return_value=forwarder),
        # Each attempt records an open and close time. The third connection is
        # healthy for 31 seconds, so its later loss starts a new retry series.
        patch(
            "colab_cli.console.time.monotonic",
            side_effect=[0, 1, 2, 3, 4, 35, 36, 37],
        ),
    ):
        _connect_as_tty(
            mock_session,
            refresh_session=MagicMock(return_value=mock_session),
            retry_delays=(1, 2),
            _max_reconnect_attempts=3,
        )

    assert forwarder.stop_event.wait.call_args_list == [call(1), call(2), call(1)]
    stderr = capsys.readouterr().err
    assert stderr.count("Console connection lost") == 3
    assert stderr.count("Reconnecting in 1s (attempt 1") == 2
    assert "Reconnecting in 2s (attempt 2" in stderr


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=False)
def test_console_piped_disconnect_is_not_retried_or_replayed(
    _mock_isatty, mock_ws_app, mock_session
):
    ws = _websocket_attempt(
        error=websocket.WebSocketConnectionClosedException("proxy link lost"),
        close_code=1006,
    )

    def make_ws(**kwargs):
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws

    with patch("colab_cli.console._ConsoleInputForwarder.start"):
        with pytest.raises(ConsoleConnectionError, match="proxy link lost"):
            connect_console(mock_session, retry_delays=(0,), _max_reconnect_attempts=1)

    assert mock_ws_app.call_count == 1


@patch("colab_cli.console.websocket.WebSocketApp")
@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_initial_proxy_auth_error_is_returned_to_outer_refresh(
    _mock_isatty, mock_ws_app, mock_session
):
    """A failed initial handshake uses State's bounded credential retry."""
    error = RuntimeError("Handshake status 401 Unauthorized")
    ws = _websocket_attempt(opened=False, error=error, close_code=None)

    def make_ws(**kwargs):
        ws._on_open = kwargs["on_open"]
        ws._on_error = kwargs["on_error"]
        ws._on_close = kwargs["on_close"]
        return ws

    mock_ws_app.side_effect = make_ws
    refresh = MagicMock()

    with pytest.raises(RuntimeError, match="Handshake status 401"):
        _connect_as_tty(
            mock_session,
            refresh_session=refresh,
            retry_delays=(0,),
            _max_reconnect_attempts=1,
        )

    assert mock_ws_app.call_count == 1
    refresh.assert_not_called()


@patch("colab_cli.console.sys.stdin.isatty", return_value=True)
def test_console_retry_limit_bounds_failures_and_releases_attempts(
    _mock_isatty, mock_session
):
    """A failed reconnect test cannot spin forever or retain every socket."""
    sockets = []

    class DisconnectingWebSocket:
        def __init__(self, **kwargs):
            self.on_open = kwargs["on_open"]
            self.on_error = kwargs["on_error"]
            self.on_close = kwargs["on_close"]
            sockets.append(weakref.ref(self))

        def run_forever(self, **_kwargs):
            self.on_open(self)
            self.on_error(
                self,
                websocket.WebSocketConnectionClosedException("proxy link lost"),
            )
            self.on_close(self, 1006, "abnormal closure")

        def send(self, _payload):
            pass

        def close(self):
            pass

    with (
        patch("colab_cli.console.websocket.WebSocketApp", DisconnectingWebSocket),
        patch("colab_cli.console.threading.Thread"),
        patch("colab_cli.console._status"),
        patch("colab_cli.console.send_terminal_size"),
        patch("colab_cli.console.logger.debug"),
        patch("colab_cli.console.logger.info"),
        pytest.raises(ConsoleConnectionError, match="reconnect limit"),
    ):
        _connect_as_tty(
            mock_session,
            refresh_session=lambda _session: mock_session,
            retry_delays=(0,),
            _max_reconnect_attempts=500,
        )

    assert len(sockets) == 501
    gc.collect()
    assert not [ref for ref in sockets if ref() is not None]


def test_console_input_forwarder_thread_stops_while_stdin_is_idle():
    """Stopping Console wakes its one stdin thread without waiting for input."""
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd) as reader,
        os.fdopen(write_fd, "w") as _writer,
        patch("colab_cli.console.sys.stdin", reader),
    ):
        forwarder = _ConsoleInputForwarder(is_tty=True)
        forwarder.start()
        deadline = time.monotonic() + 1
        while not forwarder._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)

        forwarder.stop()

        assert forwarder.join(timeout=1)


def test_console_input_forwarder_drains_buffered_input_without_waiting_for_eof():
    """Text already buffered in stdin is forwarded while the pipe stays open."""
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd) as reader,
        os.fdopen(write_fd, "w") as writer,
        patch("colab_cli.console.sys.stdin", reader),
    ):
        writer.write("abc")
        writer.flush()
        ws = MagicMock()
        forwarder = _ConsoleInputForwarder(is_tty=True)
        forwarder.attach(ws)
        forwarder.start()

        deadline = time.monotonic() + 1
        while ws.send.call_count < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        forwarder.stop()
        assert forwarder.join(timeout=1)

    assert [json.loads(call.args[0]) for call in ws.send.call_args_list] == [
        {"data": "a"},
        {"data": "b"},
        {"data": "c"},
    ]


def test_console_ctrl_c_during_reconnect_is_visible(capsys):
    """Raw-mode Ctrl-C cancellation must not look like another silent freeze."""
    forwarder = _ConsoleInputForwarder(is_tty=True)

    forwarder._forward_char("\x03")

    assert forwarder.user_requested_close is True
    assert forwarder.stop_event.is_set()
    assert "Console reconnect cancelled by user" in capsys.readouterr().err


def test_console_real_loopback_reconnect_uses_refreshed_token(
    mock_session, monkeypatch, capsys
):
    """Exercise WebSocketApp's real close/reconnect path over loopback."""
    requests = []
    ready = threading.Event()
    port = []

    def serve_two_connections():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            listener.settimeout(5)
            port.append(listener.getsockname()[1])
            ready.set()
            for close_code in (1011, 1000):
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += conn.recv(4096)
                    requests.append(request.split(b"\r\n", 1)[0].decode())
                    key = next(
                        line.split(b":", 1)[1].strip()
                        for line in request.split(b"\r\n")
                        if line.lower().startswith(b"sec-websocket-key:")
                    )
                    accept = base64.b64encode(
                        hashlib.sha1(
                            key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                        ).digest()
                    )
                    conn.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\n"
                        b"Connection: Upgrade\r\n"
                        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                    )
                    conn.sendall(b"\x88\x02" + close_code.to_bytes(2, "big"))

    with ThreadPoolExecutor(max_workers=1) as executor:
        server = executor.submit(serve_two_connections)
        assert ready.wait(2)

        for key in ("NO_PROXY", "no_proxy"):
            current = os.environ.get(key, "")
            monkeypatch.setenv(
                key, f"127.0.0.1,localhost{',' + current if current else ''}"
            )

        initial = SessionState(
            name=mock_session.name,
            token="old-token",
            url=f"http://127.0.0.1:{port[0]}",
            endpoint=mock_session.endpoint,
        )
        refreshed = SessionState(
            name=mock_session.name,
            token="fresh-token",
            url=f"http://127.0.0.1:{port[0]}",
            endpoint=mock_session.endpoint,
        )

        with (
            patch("colab_cli.console.sys.stdin.isatty", return_value=True),
            patch("colab_cli.console._ConsoleInputForwarder.start"),
            patch("colab_cli.console.sys.stdin.fileno", return_value=0),
            patch("colab_cli.console.termios.tcgetattr", return_value=["attrs"]),
            patch("colab_cli.console.termios.tcsetattr"),
            patch("colab_cli.console.tty.setraw"),
            patch("colab_cli.console.signal.getsignal", return_value=MagicMock()),
            patch("colab_cli.console.signal.signal"),
        ):
            connect_console(
                initial,
                refresh_session=MagicMock(return_value=refreshed),
                retry_delays=(0,),
                _max_reconnect_attempts=1,
            )

        # Propagate errors from the fake peer instead of leaving Console in a
        # zero-delay retry loop while pytest captures output without bounds.
        server.result(timeout=2)

    assert len(requests) == 2
    assert "colab-runtime-proxy-token=old-token" in requests[0]
    assert "colab-runtime-proxy-token=fresh-token" in requests[1]
    assert "Console reconnected" in capsys.readouterr().err


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
    mock_ws.send.assert_not_called()


@patch("colab_cli.console.sys.stdout.buffer.write")
@patch("colab_cli.console.sys.stdout.buffer.flush")
def test_on_message_acknowledges_pty_flow_control(mock_flush, mock_write):
    """The raw /colab/tty peer pauses after six unacknowledged output chunks."""
    mock_ws = MagicMock()
    message_json = json.dumps({"data": "flow-controlled output", "ack": True})
    events = []
    mock_write.side_effect = lambda _data: events.append("write")
    mock_flush.side_effect = lambda: events.append("flush")
    mock_ws.send.side_effect = lambda _payload: events.append("ack")

    on_message(mock_ws, message_json)

    mock_write.assert_called_once_with(b"flow-controlled output")
    mock_flush.assert_called_once()
    mock_ws.send.assert_called_once_with(json.dumps({"ack": True}))
    assert events == ["write", "flush", "ack"]


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
