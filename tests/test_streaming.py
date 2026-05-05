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

from unittest.mock import MagicMock
from colab_cli.runtime import ColabRuntime


def test_runtime_execute_code_streaming():
    runtime = ColabRuntime("http://url", "token")
    mock_client = MagicMock()

    # Mock the return value of execute_interactive (the raw reply)
    mock_client.execute_interactive.return_value = {"content": {"status": "ok"}}

    # Inject our mock client
    runtime._kernel_client = mock_client

    streamed_outputs = []

    def output_hook(o):
        streamed_outputs.append(o)

    code = "print(1); print(2)"

    # We need to simulate the execution where execute_interactive is called
    # and it calls our wrapped_output_hook.
    # Since wrapped_output_hook is defined inside execute_code, we have to
    # intercept the call to execute_interactive to get a reference to it.

    def side_effect(code, output_hook=None, **kwargs):
        # Simulate messages arriving
        msg1 = {
            "header": {"msg_type": "stream"},
            "content": {"name": "stdout", "text": "1\n"},
        }
        msg2 = {
            "header": {"msg_type": "stream"},
            "content": {"name": "stdout", "text": "2\n"},
        }
        if output_hook:
            output_hook(msg1)
            output_hook(msg2)
        return {"content": {"status": "ok"}}

    mock_client.execute_interactive.side_effect = side_effect

    outputs = runtime.execute_code(code, output_hook=output_hook)

    assert len(outputs) == 2
    assert outputs[0]["text"] == "1\n"
    assert outputs[1]["text"] == "2\n"

    # Verify streaming hook was called
    assert len(streamed_outputs) == 2
    assert streamed_outputs[0]["text"] == "1\n"
    assert streamed_outputs[1]["text"] == "2\n"


def test_runtime_execute_code_streaming_error_synthesis():
    runtime = ColabRuntime("http://url", "token")
    mock_client = MagicMock()

    # Simulate an error reply but NO error output message
    mock_client.execute_interactive.return_value = {
        "content": {
            "status": "error",
            "ename": "RuntimeError",
            "evalue": "something went wrong",
            "traceback": ["tb line 1"],
        }
    }
    runtime._kernel_client = mock_client

    streamed_outputs = []
    outputs = runtime.execute_code(
        "fail()", output_hook=lambda o: streamed_outputs.append(o)
    )

    # Final outputs should include synthesized error
    assert len(outputs) == 1
    assert outputs[0]["output_type"] == "error"
    assert outputs[0]["ename"] == "RuntimeError"

    # Note: synthesized error is added AFTER execute_interactive returns,
    # so it won't be in streamed_outputs unless we specifically add logic for it.
    # Currently it's only in the returned list.
    assert len(streamed_outputs) == 0
