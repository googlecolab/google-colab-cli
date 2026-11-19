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

"""A mock of jupyter-kernel-client's websocket client for streaming tests.

MockKernelClient replays a script of kernel messages on a simulated clock, so
tests can cover long runs and long silences without waiting.
"""

import itertools
import queue
import threading
from unittest.mock import MagicMock

from colab_cli import runtime as runtime_module

REQUEST = "req-1"
_msg_ids = itertools.count()


def kernel_message(msg_type, content, parent=REQUEST):
    """A Jupyter message as the websocket client delivers it."""
    return {
        "msg_type": msg_type,
        "header": {"msg_type": msg_type, "msg_id": f"msg-{next(_msg_ids)}"},
        "parent_header": {"msg_id": parent} if parent else {},
        "content": content,
    }


def stdout_message(text, parent=REQUEST):
    return kernel_message("stream", {"name": "stdout", "text": text}, parent)


def status_message(state, parent=REQUEST):
    return kernel_message("status", {"execution_state": state}, parent)


def reply_message(status="ok", parent=REQUEST, **content):
    return kernel_message("execute_reply", {"status": status, **content}, parent)


class _SimulatedEvent:
    """A threading.Event stand-in that respects the mock's simulated clock.

    When the connection drops (``is_set`` returns False), ``wait(timeout)``
    checks whether ``reconnect_at`` falls within the timeout window.  If so it
    advances the clock and re-sets the flag, simulating the WebSocket library's
    automatic reconnect.
    """

    def __init__(self, mock_client):
        self._mock = mock_client
        self._flag = True

    def is_set(self):
        return self._flag

    def set(self):
        self._flag = True

    def clear(self):
        self._flag = False

    def wait(self, timeout=None):
        if self._flag:
            return True
        ra = self._mock.reconnect_at
        if ra is not None and ra <= self._mock.now + (timeout or 0):
            self._mock.now = max(self._mock.now, ra)
            self._flag = True
            return True
        if timeout is not None:
            self._mock.now += timeout
        return False


class MockKernelClient:
    """The websocket client's methods that ColabRuntime uses, on a simulated clock.

    ``iopub`` and ``shell`` are lists of (seconds after the request, message).
    By default the shell channel replies when the last iopub message arrives. A
    get_*_msg call that would wait past the next message advances the clock by
    ``timeout`` and raises queue.Empty, as a quiet kernel would. The connection
    drops when the clock reaches ``disconnect_at`` and comes back at
    ``reconnect_at`` (if set).
    """

    def __init__(self, iopub, shell=None, disconnect_at=None, reconnect_at=None):
        self.now = 0.0
        self.iopub = list(iopub)
        last = iopub[-1][0] if iopub else 0.0
        self.shell = list(shell if shell is not None else [(last, reply_message())])
        self.disconnect_at = disconnect_at
        self.reconnect_at = reconnect_at
        self.waits = []
        self.connection_ready = _SimulatedEvent(self)
        self.connection_ready.set()

    def execute(self, code, **kwargs):
        self.code, self.execute_kwargs = code, kwargs
        return REQUEST

    def _get(self, messages, timeout):
        self.waits.append(timeout)
        assert timeout is not None and timeout > 0, "waits must be bounded"
        if messages and messages[0][0] <= self.now + timeout:
            at, msg = messages.pop(0)
            self.now = max(self.now, at)
            return msg
        self.now += timeout
        if self.disconnect_at is not None and self.now >= self.disconnect_at:
            self.connection_ready.clear()
        raise queue.Empty

    def get_iopub_msg(self, timeout=None):
        return self._get(self.iopub, timeout)

    def get_shell_msg(self, timeout=None):
        return self._get(self.shell, timeout)


def connect_runtime(runtime, mock_kc, monkeypatch):
    """Make ``runtime`` use ``mock_kc``, and read its clock from ``mock_kc``.

    Only ``runtime``'s view of ``time.monotonic`` changes; everything else in
    ``time`` stays real.
    """
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: mock_kc.now)
    runtime._kernel_client = MagicMock()
    runtime._kernel_client._manager.client = mock_kc
