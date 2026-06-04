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

from unittest.mock import MagicMock, patch, ANY

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@pytest.fixture
def mock_runtime_class(mocker):
    # Patch it in the command module where it's used
    return mocker.patch("colab_cli.commands.execution.ColabRuntime")


def test_cli_exec_file(mock_store, mock_runtime_class, mock_common_state, tmp_path):
    mock_session = MagicMock()
    mock_session.url = "http://url"
    mock_session.token = "token123"
    mock_session.name = "s1"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "hello\n"}]

    script = tmp_path / "script.py"
    script.write_text("print('hello')")

    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(script)])
    assert result.exit_code == 0
    assert mock_session.last_execution[0] == str(script)
    assert mock_session.last_execution[1] is None
    assert mock_session.last_execution[2] is not None
    mock_store.add.assert_called_with(mock_session)
    mock_runtime.execute_code.assert_any_call(
        "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
    )
    mock_runtime.execute_code.assert_any_call("print('hello')", output_hook=ANY, timeout=10.0)


def test_cli_exec_stdin(mock_store, mock_runtime_class, mock_common_state):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.url = "http://url"
    mock_session.token = "token"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"data": {"text/plain": "42"}}]

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print(42)")
    assert result.exit_code == 0
    assert mock_session.last_execution[0] == "stdin"
    assert mock_session.last_execution[1] is None
    assert mock_session.last_execution[2] is not None
    mock_store.add.assert_called_with(mock_session)
    mock_runtime.execute_code.assert_any_call("print(42)", output_hook=ANY, timeout=10.0)


def test_cli_exec_not_found(mock_common_state):
    # Case where resolve_session fails
    mock_common_state.resolve_session.side_effect = SystemExit(1)
    result = runner.invoke(app, ["exec", "-s", "missing"])
    assert result.exit_code == 1


def test_cli_exec_no_input(mock_store, mock_common_state, mocker):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"

    # Mock is_stdin_tty to True to trigger the "No input provided" error
    mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=True)

    result = runner.invoke(app, ["exec", "-s", "s1"])
    assert result.exit_code == 1
    assert "No input provided" in result.output


@patch("colab_cli.commands.execution.handle_image")
def test_cli_exec_outputs(
    mock_handle_image, mock_store, mock_runtime_class, mock_common_state
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.url = "http://url"
    mock_session.token = "token"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value

    # We need to simulate the output_hook being called because the command now relies on it
    # for immediate output, although it also returns the final list.
    def mock_execute_code(code, output_hook=None, **kwargs):
        outputs = [
            {"data": {"image/png": "png_data"}},
            {"data": {"image/jpeg": "jpeg_data"}},
            {"output_type": "error", "ename": "ValueError", "evalue": "bad"},
            {"output_type": "error", "traceback": ["line1\n", "line2\n"]},
        ]
        if output_hook:
            for o in outputs:
                output_hook(o)
        return outputs

    mock_runtime.execute_code.side_effect = mock_execute_code

    result = runner.invoke(app, ["exec", "-s", "s1"], input="do_stuff()")
    assert result.exit_code == 0

    mock_handle_image.assert_any_call("png_data", "image/png", target_path=None)
    mock_handle_image.assert_any_call("jpeg_data", "image/jpeg", target_path=None)

    assert "ValueError: bad\n" in result.stderr
    assert "line1\nline2\n" in result.stderr


def test_cli_exec_empty_code(mock_runtime_class, mock_store, mock_common_state):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.url = "http://url"
    mock_session.token = "token"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["exec", "-s", "s1"], input="   \n  ")
    assert result.exit_code == 0


def test_cli_exec_lost_session_prunes(
    mock_runtime_class, mock_store, mock_common_state
):
    mock_session = MagicMock()
    mock_session.name = "lost-sess"
    mock_store.get.return_value = mock_session
    mock_common_state.resolve_session.return_value = "lost-sess"

    mock_runtime = mock_runtime_class.return_value
    # Simulate 404 during initialization
    mock_runtime.execute_code.side_effect = Exception("404 Not Found")

    result = runner.invoke(app, ["exec", "-s", "lost-sess"], input="print(1)")
    assert result.exit_code == 1
    assert "appears to be lost" in result.output
    mock_common_state.prune_session.assert_called_once_with("lost-sess")


def test_cli_exec_timeout(mock_store, mock_runtime_class, mock_common_state, tmp_path):
    mock_session = MagicMock()
    mock_session.url = "http://url"
    mock_session.token = "token123"
    mock_session.name = "s1"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "hello\n"}]

    script = tmp_path / "script.py"
    script.write_text("print('hello')")

    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(script), "--timeout", "3600"])
    assert result.exit_code == 0
    mock_runtime.execute_code.assert_any_call("print('hello')", output_hook=ANY, timeout=3600.0)
