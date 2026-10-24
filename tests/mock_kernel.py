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

import queue
import threading
import time
import types
from unittest.mock import MagicMock

from colab_cli import runtime as runtime_module

REQUEST = "req-1"


def _msg(msg_type, content, parent=REQUEST):
    return {
        "header": {"msg_type": msg_type},
        "parent_header": {"msg_id": parent},
        "content": content,
    }


def _stdout(text, parent=REQUEST):
    return _msg("stream", {"name": "stdout", "text": text}, parent)


def _idle(parent=REQUEST):
    return _msg("status", {"execution_state": "idle"}, parent)


def _reply(status="ok", **content):
    return _msg("execute_reply", {"status": status, **content})


class MockKernelClient:
    """The websocket client's public API, on a simulated clock.

    `iopub` and `shell` are lists of (seconds after the request, message). A
    get_*_msg call that would wait past the next message advances the clock by
    `timeout` and raises queue.Empty, as a quiet kernel would.
    """

    def __init__(self, iopub, shell=None):
        self.now = 0.0
        self.iopub = list(iopub)
        self.shell = list(
            shell if shell is not None else [(self._last(iopub), _reply())]
        )
        self.waits = []
        self.connection_ready = threading.Event()
        self.connection_ready.set()

    @staticmethod
    def _last(messages):
        return messages[-1][0] if messages else 0.0

    def execute(self, code, **kwargs):
        self.code, self.execute_kwargs = code, kwargs
        return REQUEST

    def _get(self, messages, timeout):
        self.waits.append(timeout)
        assert timeout is not None and timeout > 0, (
            "must wait in bounded, non-zero slices"
        )
        if messages and messages[0][0] <= self.now + timeout:
            at, msg = messages.pop(0)
            self.now = max(self.now, at)
            return msg
        self.now += timeout
        raise queue.Empty

    def get_iopub_msg(self, timeout=None):
        return self._get(self.iopub, timeout)

    def get_shell_msg(self, timeout=None):
        return self._get(self.shell, timeout)


def connect_runtime(runtime, mock_kc, monkeypatch):
    """Make `runtime` talk to `mock_kc`, with the runtime's clock set to its clock."""
    clock = types.SimpleNamespace(monotonic=lambda: mock_kc.now, sleep=time.sleep)
    monkeypatch.setattr(runtime_module, "time", clock)
    runtime._kernel_client = MagicMock()
    runtime._kernel_client._manager.client = mock_kc
