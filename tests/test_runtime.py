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

from unittest.mock import MagicMock, patch

import jupyter_kernel_client

from colab_cli.runtime import ColabRuntime


@patch("colab_cli.runtime.jupyter_kernel_client.KernelClient")
def test_colab_runtime_kernel_client(mock_kc_cls):
    mock_kc = mock_kc_cls.return_value

    runtime = ColabRuntime("http://url", "token123")

    assert runtime._kernel_client is None

    kc = runtime.kernel_client

    mock_kc_cls.assert_called_once_with(
        server_url="http://url",
        token="token123",
        kernel_id=None,
        client_kwargs={
            "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
            "extra_params": {"colab-runtime-proxy-token": "token123"},
        },
        headers={
            "X-Colab-Client-Agent": "colab-cli",
            "X-Colab-Runtime-Proxy-Token": "token123",
        },
    )
    mock_kc.start.assert_called_once()
    assert kc == mock_kc


def test_colab_runtime_execute_code():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    # Test empty reply
    mock_kc.execute.return_value = {}
    assert runtime.execute_code("print(1)") == []

    # Test normal reply
    mock_kc.execute.return_value = {"outputs": [{"text": "1\n"}]}
    assert runtime.execute_code("print(1)") == [{"text": "1\n"}]

    # Test error status without error output
    mock_kc.execute.return_value = {
        "status": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "outputs": [{"text": "partial"}],
    }
    outputs = runtime.execute_code("raise ValueError")
    assert len(outputs) == 2
    assert outputs[0] == {"text": "partial"}
    assert outputs[1] == {
        "output_type": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "traceback": [],
    }


def test_colab_runtime_stop():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    runtime.stop()
    mock_kc._manager.client.stop_channels.assert_called_once()


def test_colab_runtime_stop_exception(caplog):
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    mock_kc._manager.client.stop_channels.side_effect = Exception("Stop failed")
    runtime._kernel_client = mock_kc

    runtime.stop()  # Should not raise
    assert "Error stopping kernel client" in caplog.text


def test_colab_runtime_stdin_logging():
    mock_history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=mock_history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute.side_effect = lambda code, allow_stdin=False, stdin_hook=None: {
        "outputs": [{"text": stdin_hook("Enter something: ")}]
    }

    with patch("colab_cli.runtime.input", return_value="user input"):
        outputs = runtime.execute_code("code", allow_stdin=True)

    assert outputs == [{"text": "user input"}]
    mock_history.log_event.assert_any_call(
        "test-s", "stdin_request", {"prompt": "Enter something: "}
    )
    mock_history.log_event.assert_any_call(
        "test-s", "input_reply", {"value": "user input"}
    )
