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

import codecs
import itertools
import json
import logging
import os
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import websocket

from colab_cli.state import SessionState
from colab_cli.utils import is_runtime_proxy_error

logger = logging.getLogger(__name__)

# These globals and module-level callbacks are retained for compatibility with
# callers that exercise the individual callback functions. ``connect_console``
# uses connection-local callbacks so multiple Console processes cannot share
# lifecycle state.
_is_running = False
_last_error = None

# When stdin is piped and reaches EOF, let the remote shell flush its goodbye
# before the client closes the websocket.
PIPED_EOF_GRACE_SECONDS = 0.5

# Protocol pings keep an otherwise idle TTY tunnel visible to HTTP proxies and
# bound how long a silently dead connection can look healthy.
CONSOLE_PING_INTERVAL_SECONDS = 20
CONSOLE_PING_TIMEOUT_SECONDS = 10

# Retry quickly through brief proxy churn, then settle into a low-frequency
# retry cadence until the user cancels or Colab confirms that the VM is gone.
CONSOLE_RETRY_DELAYS_SECONDS = (1, 2, 5, 10, 30)

# Shell-exit input is only a hint: ``exit`` may leave a nested shell and
# Ctrl-D may close a foreground program without closing the Console transport.
# Suppress reconnect only when the peer closes immediately after that input.
CONSOLE_SHELL_EXIT_INTENT_SECONDS = 2

# Treat an established socket that survives this long as a recovered
# connection. A later outage starts a fresh backoff sequence instead of
# inheriting the retry delay from an unrelated earlier outage.
CONSOLE_STABLE_CONNECTION_SECONDS = 30

_NORMAL_CLOSE_CODES = (1000, 1001)


class ConsoleConnectionError(RuntimeError):
    """The Console transport failed before piped input completed."""


def on_message(ws, message):
    """Compatibility callback for writing remote terminal output."""
    _write_terminal_message(ws, message)


def on_error(ws, error):
    """Compatibility callback for recording a websocket error."""
    global _last_error
    _last_error = error
    logger.error("WebSocket Error: %s", error)


def on_close(ws, close_status_code, close_msg):
    """Compatibility callback for recording websocket closure."""
    global _is_running
    _is_running = False


def send_terminal_size(ws):
    """Sends the current terminal size to the remote backend."""
    try:
        size = os.get_terminal_size()
        payload = json.dumps({"cols": size.columns, "rows": size.lines})
        ws.send(payload)
    except Exception as e:
        logger.debug("Failed to send terminal size: %s", e)


def on_open(ws):
    """Compatibility callback that opens one legacy stdin forwarding thread."""
    global _is_running
    _is_running = True
    send_terminal_size(ws)

    def read_stdin():
        is_tty = sys.stdin.isatty()
        while _is_running:
            try:
                char = sys.stdin.read(1)
                if not char:
                    if not is_tty:
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

    threading.Thread(target=read_stdin, daemon=True).start()


def _write_terminal_message(ws, message) -> None:
    """Writes terminal output and acknowledges the server's PTY flow control."""
    try:
        data = json.loads(message)
        if "data" in data:
            sys.stdout.buffer.write(data["data"].encode("utf-8"))
            sys.stdout.buffer.flush()
        if data.get("ack") is True:
            ws.send(json.dumps({"ack": True}))
    except Exception as e:
        logger.debug("Error handling Console message: %s", e)


def _status(message: str) -> None:
    """Writes connection state outside the remote terminal byte stream."""
    print(f"\r\n[colab] {message}", file=sys.stderr, flush=True)


def _retry_delays(delays: Iterable[float]) -> Iterator[float]:
    configured = tuple(delays)
    if not configured:
        configured = (30,)
    yield from configured
    yield from itertools.repeat(configured[-1])


@dataclass
class _Attempt:
    opened: bool = False
    error: Optional[object] = None
    received_close_frame: bool = False
    close_code: Optional[int] = None
    close_reason: str = ""
    opened_at: Optional[float] = None
    duration: float = 0.0

    @property
    def abnormal(self) -> bool:
        # websocket-client 1.9 invokes on_error with the received ABNF close
        # frame before on_close. A normal close code must win over that
        # compatibility quirk or `exit` would spuriously reconnect.
        if self.close_code in _NORMAL_CLOSE_CODES:
            return False
        if self.error is not None:
            return True
        if self.received_close_frame:
            return True
        if self.close_code is None:
            # A mocked/no-op WebSocketApp has no callbacks. A real peer close
            # invokes on_close and supplies either a code or an error callback.
            return False
        return True

    def description(self) -> str:
        if self.error is not None:
            return str(self.error)
        if self.close_code is not None:
            reason = f" ({self.close_reason})" if self.close_reason else ""
            return f"WebSocket closed with code {self.close_code}{reason}"
        return "WebSocket connection closed"


class _ConsoleInputForwarder:
    """Owns the command's single stdin reader across websocket reconnects."""

    def __init__(self, is_tty: bool):
        self.is_tty = is_tty
        self.stop_event = threading.Event()
        self._user_requested_close = False
        self._shell_close_intent_deadline: Optional[float] = None
        self._active_event = threading.Event()
        self._lock = threading.Lock()
        self._ws = None
        self._started = False
        self._thread: Optional[threading.Thread] = None
        self._line: list[str] = []
        self._stdin = sys.stdin
        try:
            self._stdin_fd: Optional[int] = self._stdin.fileno()
        except (AttributeError, OSError, TypeError, ValueError):
            self._stdin_fd = None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._read_stdin, daemon=True)
        self._thread.start()

    def attach(self, ws) -> None:
        with self._lock:
            self._ws = ws
            self._active_event.set()

    def detach(self, ws) -> None:
        with self._lock:
            if self._ws is ws:
                self._ws = None
                self._active_event.clear()

    def close_active(self) -> None:
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        self._active_event.set()

    def join(self, timeout: Optional[float] = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _current_ws(self):
        with self._lock:
            return self._ws

    def _wait_for_first_connection(self) -> bool:
        while not self.stop_event.is_set():
            if self._active_event.wait(0.1):
                return True
        return False

    @property
    def user_requested_close(self) -> bool:
        """Whether this close should terminate rather than reconnect."""
        if self._user_requested_close:
            return True
        deadline = self._shell_close_intent_deadline
        if deadline is None:
            return False
        if time.monotonic() <= deadline:
            return True
        self._shell_close_intent_deadline = None
        return False

    @user_requested_close.setter
    def user_requested_close(self, value: bool) -> None:
        # Retain the attribute-style API for permanent cancellation paths and
        # compatibility with existing callers/tests. Shell command detection
        # uses the bounded intent helper below instead.
        self._user_requested_close = value
        if not value:
            self._shell_close_intent_deadline = None

    def _mark_shell_close_intent(self) -> None:
        self._shell_close_intent_deadline = (
            time.monotonic() + CONSOLE_SHELL_EXIT_INTENT_SECONDS
        )

    def _track_exit_request(self, char: str) -> None:
        if char in ("\r", "\n"):
            command = "".join(self._line).strip()
            if command in ("exit", "logout") or command.startswith("exit "):
                self._mark_shell_close_intent()
            self._line.clear()
        elif char in ("\x7f", "\b"):
            if self._line:
                self._line.pop()
        elif char == "\x04":
            self._mark_shell_close_intent()
        elif char.isprintable():
            self._line.append(char)

    def _read_stdin(self) -> None:
        # Piped bytes must not be consumed until the first socket is ready.
        if not self.is_tty and not self._wait_for_first_connection():
            return

        if self._stdin_fd is None:
            self._read_text_stdin()
            return

        encoding = getattr(self._stdin, "encoding", None) or "utf-8"
        errors = getattr(self._stdin, "errors", None) or "strict"
        decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        while not self.stop_event.is_set():
            try:
                readable, _, _ = select.select([self._stdin_fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(self._stdin_fd, 4096)
            except (OSError, TypeError, ValueError):
                return

            if not chunk:
                tail = decoder.decode(b"", final=True)
                for char in tail:
                    self._forward_char(char)
                self._handle_eof()
                return

            for char in decoder.decode(chunk):
                self._forward_char(char)

    def _read_text_stdin(self) -> None:
        """Fallback for synthetic streams without a selectable file descriptor."""
        while not self.stop_event.is_set():
            try:
                char = self._stdin.read(1)
            except Exception:
                return
            if not char:
                self._handle_eof()
                return
            self._forward_char(char)

    def _handle_eof(self) -> None:
        if self.is_tty:
            return
        self.user_requested_close = True
        ws = self._current_ws()
        if ws is not None:
            try:
                ws.send(json.dumps({"data": "exit\n"}))
            except Exception:
                pass
            time.sleep(PIPED_EOF_GRACE_SECONDS)
            try:
                ws.close()
            except Exception:
                pass
        self.stop_event.set()

    def _forward_char(self, char: str) -> None:
        if self.stop_event.is_set():
            return
        ws = self._current_ws()
        if ws is None:
            # Raw mode suppresses SIGINT. During a reconnect delay, make
            # Ctrl-C an explicit request to stop retrying.
            if self.is_tty and char == "\x03":
                self.user_requested_close = True
                self.stop_event.set()
                _status("Console reconnect cancelled by user.")
            return

        if self.is_tty:
            self._track_exit_request(char)
        try:
            ws.send(json.dumps({"data": char}))
        except Exception:
            # The websocket callback/loop owns reconnect decisions.
            pass


def _build_ws_url(session: SessionState) -> str:
    parsed = urlparse(session.url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return (
        f"{ws_scheme}://{parsed.netloc}/colab/tty"
        f"?colab-runtime-proxy-token={session.token}"
    )


def connect_console(
    session: SessionState,
    *,
    refresh_session: Optional[Callable[[SessionState], Optional[SessionState]]] = None,
    retry_delays: Iterable[float] = CONSOLE_RETRY_DELAYS_SECONDS,
    _max_reconnect_attempts: Optional[int] = None,
) -> None:
    """Connects to the Colab TTY and reconnects an interrupted TTY session.

    Reconnection never creates, stops, or replaces a runtime. The optional
    refresh callback must return credentials for the same endpoint, ``None``
    when the control plane confirms it is gone, or raise when the lookup is
    inconclusive.
    """
    is_tty = sys.stdin.isatty()
    fd = sys.stdin.fileno() if is_tty else None
    old_settings = termios.tcgetattr(fd) if is_tty else None
    old_sigwinch = signal.getsignal(signal.SIGWINCH) if is_tty else None
    forwarder = _ConsoleInputForwarder(is_tty)
    current = session
    original_endpoint = session.endpoint
    delays = _retry_delays(retry_delays)
    reconnect_attempt = 0
    total_reconnect_attempts = 0
    active_ws = {"ws": None}
    pid = os.getpid()

    def handle_sigwinch(signum, frame):
        ws = active_ws["ws"]
        if ws is not None:
            send_terminal_size(ws)

    try:
        if is_tty:
            tty.setraw(fd, termios.TCSANOW)
            signal.signal(signal.SIGWINCH, handle_sigwinch)
        while not forwarder.stop_event.is_set():
            attempt = _Attempt()
            ws_url = _build_ws_url(current)

            def attempt_open(ws):
                attempt.opened = True
                attempt.opened_at = time.monotonic()
                active_ws["ws"] = ws
                forwarder.attach(ws)
                forwarder.start()
                send_terminal_size(ws)
                logger.info(
                    "Console connected pid=%s endpoint=%s reconnect_attempt=%s",
                    pid,
                    original_endpoint,
                    reconnect_attempt,
                )
                if reconnect_attempt:
                    _status(
                        f"Console reconnected (attempt {reconnect_attempt}, "
                        f"endpoint {original_endpoint})."
                    )

            def attempt_message(ws, message):
                _write_terminal_message(ws, message)

            def attempt_error(ws, error):
                # websocket-client 1.9 passes a received ABNF close frame to
                # on_error, then invokes on_close with a lost/None status. Parse
                # that frame here so normal 1000 closes do not reconnect while
                # abnormal/empty closes do.
                if (
                    isinstance(error, websocket.ABNF)
                    and error.opcode == websocket.ABNF.OPCODE_CLOSE
                ):
                    attempt.received_close_frame = True
                    if len(error.data) >= 2:
                        attempt.close_code = struct.unpack("!H", error.data[:2])[0]
                        attempt.close_reason = error.data[2:].decode(
                            "utf-8", errors="replace"
                        )
                else:
                    attempt.error = error
                logger.debug(
                    "Console WebSocket error pid=%s endpoint=%s "
                    "reconnect_attempt=%s error=%s",
                    pid,
                    original_endpoint,
                    reconnect_attempt,
                    error,
                )

            def attempt_close(ws, close_status_code, close_msg):
                attempt.received_close_frame = True
                if close_status_code is not None:
                    attempt.close_code = close_status_code
                    attempt.close_reason = close_msg or ""
                active_ws["ws"] = None
                forwarder.detach(ws)
                attempt.duration = (
                    time.monotonic() - attempt.opened_at
                    if attempt.opened_at is not None
                    else 0.0
                )
                logger.info(
                    "Console closed pid=%s endpoint=%s reconnect_attempt=%s "
                    "code=%s reason=%r duration=%.1fs",
                    pid,
                    original_endpoint,
                    reconnect_attempt,
                    close_status_code,
                    close_msg or "",
                    attempt.duration,
                )

            ws = websocket.WebSocketApp(
                url=ws_url,
                on_open=attempt_open,
                on_message=attempt_message,
                on_error=attempt_error,
                on_close=attempt_close,
            )
            active_ws["ws"] = ws
            try:
                ws.run_forever(
                    ping_interval=CONSOLE_PING_INTERVAL_SECONDS,
                    ping_timeout=CONSOLE_PING_TIMEOUT_SECONDS,
                )
            finally:
                active_ws["ws"] = None
                forwarder.detach(ws)

            if forwarder.user_requested_close or not attempt.abnormal:
                break

            if attempt.opened and attempt.duration >= CONSOLE_STABLE_CONNECTION_SECONDS:
                reconnect_attempt = 0
                delays = _retry_delays(retry_delays)

            if not attempt.opened and isinstance(attempt.error, Exception):
                if is_runtime_proxy_error(attempt.error):
                    # Let State's bounded startup retry refresh an expired
                    # runtime-proxy token. Generic Console reconnects are for
                    # established sockets, not authentication failures.
                    raise attempt.error

            if not is_tty:
                raise ConsoleConnectionError(attempt.description())

            if (
                _max_reconnect_attempts is not None
                and total_reconnect_attempts >= _max_reconnect_attempts
            ):
                raise ConsoleConnectionError(
                    "Console reconnect limit reached after "
                    f"{total_reconnect_attempts} attempt(s): "
                    f"{attempt.description()}"
                )

            _status(f"Console connection lost: {attempt.description()}.")

            reconnect_attempt += 1
            total_reconnect_attempts += 1
            delay = next(delays)
            _status(
                f"Reconnecting in {delay:g}s (attempt {reconnect_attempt}; "
                "press Ctrl-C to stop)..."
            )
            if forwarder.stop_event.wait(delay):
                break

            if refresh_session is not None:
                try:
                    refreshed = refresh_session(current)
                except Exception as error:
                    logger.debug(
                        "Console credential refresh failed pid=%s endpoint=%s "
                        "reconnect_attempt=%s error=%s",
                        pid,
                        original_endpoint,
                        reconnect_attempt,
                        error,
                    )
                    _status(
                        "Warning: could not refresh credentials; retrying with "
                        f"the last known token ({error})."
                    )
                else:
                    if refreshed is None:
                        _status(
                            f"Session '{session.name}' is no longer active; "
                            "stopping Console reconnects."
                        )
                        break
                    if refreshed.endpoint != original_endpoint:
                        _status(
                            f"Session '{session.name}' now refers to a different "
                            "runtime; refusing to reconnect to it."
                        )
                        break
                    current = refreshed
    except KeyboardInterrupt:
        forwarder.user_requested_close = True
    finally:
        forwarder.stop()
        forwarder.close_active()
        forwarder.join(timeout=0.2)
        if is_tty:
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            signal.signal(signal.SIGWINCH, old_sigwinch)
        _status("Console connection closed.")
