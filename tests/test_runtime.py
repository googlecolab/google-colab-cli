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
import pytest
from mock_kernel import (
    MockKernelClient,
    connect_runtime,
    reply_message,
    status_message,
    stdout_message,
)

from colab_cli.runtime import ColabRuntime


def test_colab_runtime_kernel_client():
    if hasattr(jupyter_kernel_client, "ColabKernelClient"):
        target_attr = "ColabKernelClient"
        token_param_name = "proxy_token"
    elif hasattr(jupyter_kernel_client, "KernelClient"):
        target_attr = "KernelClient"
        token_param_name = "token"
    else:
        target_attr = "JupyterKernelClient"
        token_param_name = "token"

    with patch.object(jupyter_kernel_client, target_attr) as mock_kc_cls:
        mock_kc = mock_kc_cls.return_value
        runtime = ColabRuntime("http://url", "token123")

        assert runtime._kernel_client is None

        kc = runtime.kernel_client

        expected_kwargs = {
            "server_url": "http://url",
            token_param_name: "token123",
            "kernel_id": None,
            "client_kwargs": {
                "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
                "extra_params": {"colab-runtime-proxy-token": "token123"},
                "reconnect_interval": 1,
            },
            "headers": {
                "X-Colab-Client-Agent": "colab-cli",
                "X-Colab-Runtime-Proxy-Token": "token123",
            },
        }
        mock_kc_cls.assert_called_once_with(**expected_kwargs)
        mock_kc.start.assert_called_once()
        assert kc == mock_kc


def test_colab_runtime_kernel_client_jupyterclient_fallback():
    """PyPI jupyter-kernel-client>=1.0 renamed KernelClient to
    JupyterKernelClient. Runtime must fall back to it instead of raising
    AttributeError (breaks `colab exec` on `uv tool install google-colab-cli`)."""
    import colab_cli.runtime as runtime_mod

    real_colab = getattr(jupyter_kernel_client, "ColabKernelClient", None)
    real_legacy = getattr(jupyter_kernel_client, "KernelClient", None)
    had_colab = hasattr(jupyter_kernel_client, "ColabKernelClient")
    had_legacy = hasattr(jupyter_kernel_client, "KernelClient")

    if had_colab:
        delattr(jupyter_kernel_client, "ColabKernelClient")
    if had_legacy:
        delattr(jupyter_kernel_client, "KernelClient")
    try:
        with patch.object(
            jupyter_kernel_client, "JupyterKernelClient", create=True
        ) as mock_kc_cls:
            mock_kc = mock_kc_cls.return_value
            runtime = ColabRuntime("http://url", "token123")
            kc = runtime.kernel_client
            mock_kc_cls.assert_called_once_with(
                server_url="http://url",
                token="token123",
                kernel_id=None,
                client_kwargs={
                    "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
                    "extra_params": {"colab-runtime-proxy-token": "token123"},
                    "reconnect_interval": 1,
                },
                headers={
                    "X-Colab-Client-Agent": "colab-cli",
                    "X-Colab-Runtime-Proxy-Token": "token123",
                },
            )
            mock_kc.start.assert_called_once()
            assert kc == mock_kc
    finally:
        if had_colab:
            jupyter_kernel_client.ColabKernelClient = real_colab
        if had_legacy:
            jupyter_kernel_client.KernelClient = real_legacy
        assert runtime_mod  # keep import used for clarity


def test_colab_runtime_execute_code(monkeypatch):
    runtime = ColabRuntime("http://url", "token123")

    # Test empty output without output_hook
    mock_kc = MockKernelClient([(0.1, status_message("idle"))])
    connect_runtime(runtime, mock_kc, monkeypatch)
    assert runtime.execute_code("pass") == []

    # Test normal output without output_hook
    mock_kc = MockKernelClient(
        [(0.1, stdout_message("1\n")), (0.2, status_message("idle"))]
    )
    connect_runtime(runtime, mock_kc, monkeypatch)
    assert runtime.execute_code("print(1)") == [
        {"output_type": "stream", "name": "stdout", "text": "1\n"}
    ]

    # Test error status without error output
    reply = reply_message("error", ename="ValueError", evalue="bad", traceback=[])
    mock_kc = MockKernelClient(
        [(0.1, stdout_message("partial")), (0.2, status_message("idle"))],
        shell=[(0.2, reply)],
    )
    connect_runtime(runtime, mock_kc, monkeypatch)
    outputs = runtime.execute_code("raise ValueError")
    assert len(outputs) == 2
    assert outputs[0] == {"output_type": "stream", "name": "stdout", "text": "partial"}
    assert outputs[1] == {
        "output_type": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "traceback": [],
    }


def test_colab_runtime_execute_code_allow_stdin_default_no_timeout():
    """By default with allow_stdin=True, execute_code should NOT pass a timeout
    (relies on jupyter kernel client default)."""
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute.return_value = {"outputs": []}
    runtime.execute_code("print(1)", allow_stdin=True)

    _, kwargs = mock_kc.execute.call_args
    assert "timeout" not in kwargs


def test_colab_runtime_execute_code_allow_stdin_with_timeout():
    """When a timeout is supplied with allow_stdin=True, it must be forwarded
    to kernel_client.execute."""
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute.return_value = {"outputs": []}
    runtime.execute_code("print(1)", allow_stdin=True, timeout=600)

    _, kwargs = mock_kc.execute.call_args
    assert kwargs.get("timeout") == 600


def test_colab_runtime_execute_code_allow_stdin_with_output_hook_not_implemented():
    """Combining allow_stdin=True and output_hook raises NotImplementedError."""
    runtime = ColabRuntime("http://url", "token123")
    with pytest.raises(NotImplementedError):
        runtime.execute_code(
            "print(1)", output_hook=lambda o: None, allow_stdin=True, timeout=600
        )


@pytest.mark.parametrize("output_hook", [None, lambda o: None])
def test_colab_runtime_streaming_timeout_is_an_idle_limit(monkeypatch, output_hook):
    """Without stdin, execute_code streams through _execute_streaming (with or
    without output_hook), where timeout is the longest silence allowed between
    messages."""
    for silence, finishes in ((599, True), (601, False)):
        runtime = ColabRuntime("http://url", "token123")
        mock_kc = MockKernelClient(
            [(silence, stdout_message("1\n")), (silence + 0.5, status_message("idle"))]
        )
        connect_runtime(runtime, mock_kc, monkeypatch)
        if finishes:
            outputs = runtime.execute_code(
                "print(1)", output_hook=output_hook, timeout=600
            )
            assert outputs[0]["text"] == "1\n"
        else:
            with pytest.raises(TimeoutError):
                runtime.execute_code("print(1)", output_hook=output_hook, timeout=600)


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
