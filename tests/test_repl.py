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
from colab_cli.repl import ColabREPL

runner = CliRunner()


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@pytest.fixture
def mock_runtime_class(mocker):
    # Patch it in the command module where it's used
    return mocker.patch("colab_cli.commands.execution.ColabRuntime")


@patch("colab_cli.repl.handle_image")
def test_repl_display_output(mock_handle_image, capsys):
    runtime = MagicMock()
    repl_inst = ColabREPL(runtime)

    outputs = [
        {"text": "hello"},
        {"data": {"image/png": "png_data", "text/plain": "<Figure size>"}},
        {"data": {"image/jpeg": "jpeg_data", "text/plain": "other_text"}},
        {"output_type": "error", "ename": "ValueError", "evalue": "bad"},
        {"output_type": "error", "traceback": ["line1\n", "line2\n"]},
    ]

    for o in outputs:
        repl_inst.display_output(o)

    mock_handle_image.assert_any_call("png_data", "image/png", target_path=None)
    mock_handle_image.assert_any_call("jpeg_data", "image/jpeg", target_path=None)

    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "other_text" in captured.out
    assert "ValueError: bad" in captured.out
    assert "line1\nline2\n" in captured.out


def test_repl_execute(mock_store, mock_common_state):
    runtime = MagicMock()

    mock_session = MagicMock()
    mock_store.get.return_value = mock_session

    def mock_execute_code(code, output_hook=None, **kwargs):
        o = {"text": "done"}
        if output_hook:
            output_hook(o)
        return [o]

    runtime.execute_code.side_effect = mock_execute_code
    repl_inst = ColabREPL(runtime, session_name="s1")

    with patch.object(repl_inst, "display_output") as mock_display:
        repl_inst.execute("print(1)")
        mock_display.assert_called_once_with({"text": "done"})
        assert repl_inst.repl_history[0]["input"] == "print(1)"

        assert mock_session.last_execution[0] == "REPL"
        assert mock_session.last_execution[1] is None
        assert mock_session.last_execution[2] is not None
        mock_store.add.assert_called_with(mock_session)


def test_cli_repl_interactive(
    mock_runtime_class, mock_store, mock_common_state, mocker
):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.url = "http://url"
    mock_session.token = "token"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"

    # Mock is_stdin_tty to True to follow the interactive path
    mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=True)

    # Simulate TTY for interactive REPL
    with patch("colab_cli.repl.ColabREPL") as mock_repl_class:
        # Mock run to prevent infinite loop or errors
        mock_repl_class.return_value.run.return_value = None
        runner.invoke(app, ["repl", "-s", "s1"])
        assert mock_repl_class.called


def test_cli_repl_piped(mock_runtime_class, mock_store, mock_common_state):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.url = "http://url"
    mock_session.token = "token"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "done piped"}]

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["repl", "-s", "s1"], input="print(1)")
    assert result.exit_code == 0
    assert mock_session.last_execution[0] == "stdin"
    assert mock_session.last_execution[2] is not None
    mock_store.add.assert_called_with(mock_session)
    mock_runtime.execute_code.assert_any_call("print(1)", output_hook=ANY)


def test_cli_repl_missing_session(mock_common_state):
    mock_common_state.resolve_session.side_effect = SystemExit(1)
    result = runner.invoke(app, ["repl", "-s", "missing"])
    assert result.exit_code == 1


def test_cli_repl_piped_empty(mock_runtime_class, mock_store, mock_common_state):
    mock_session = MagicMock()
    mock_session.name = "s1"
    mock_session.url = "http://url"
    mock_session.token = "token"
    mock_session.kernel_id = None
    mock_session.session_id = None
    mock_store.get.return_value = mock_session

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["repl", "-s", "s1"], input="   \n  ")
    assert result.exit_code == 0


def test_repl_print_info_error(capsys):
    repl_inst = ColabREPL(MagicMock())
    repl_inst.print_info("info_msg")
    repl_inst.print_error("err_msg")


@patch("colab_cli.repl.handle_image")
def test_repl_display_output_image_suppress_text(mock_handle_image, capsys):
    repl_inst = ColabREPL(MagicMock())
    output = {"data": {"image/png": "png_data", "text/plain": "<Figure size>"}}
    repl_inst.display_output(output)
    mock_handle_image.assert_called_once_with("png_data", "image/png", target_path=None)

    captured = capsys.readouterr()
    assert "<Figure size>" not in captured.out


def test_repl_execute_error(capsys):
    runtime = MagicMock()
    runtime.execute_code.side_effect = Exception("Kernel ded")
    repl_inst = ColabREPL(runtime)

    repl_inst.execute("print(1)")

    captured = capsys.readouterr()
    assert "Kernel ded" in captured.out


def test_repl_run_quit_aliases(mocker):
    runtime = MagicMock()
    repl_inst = ColabREPL(runtime)
    repl_inst.session = MagicMock()

    # Test /quit
    repl_inst.session.prompt.side_effect = ["/quit"]
    repl_inst.run()
    assert runtime.stop.called

    # Test quit()
    runtime.stop.reset_mock()
    repl_inst.session.prompt.side_effect = ["quit()"]
    repl_inst.run()
    assert runtime.stop.called

    # Test exit()
    runtime.stop.reset_mock()
    repl_inst.session.prompt.side_effect = ["exit()"]
    repl_inst.run()
    assert runtime.stop.called


def test_repl_run_misc_inputs(mocker, capsys):
    runtime = MagicMock()
    repl_inst = ColabREPL(runtime)
    repl_inst.session = MagicMock()

    # None result, empty string, then exit()
    repl_inst.session.prompt.side_effect = [None, "   ", "exit()"]
    repl_inst.run()
    assert runtime.stop.called


def test_repl_run_exceptions(mocker, capsys):
    runtime = MagicMock()
    repl_inst = ColabREPL(runtime)
    repl_inst.session = MagicMock()

    # EOFError
    repl_inst.session.prompt.side_effect = [EOFError()]
    repl_inst.run()
    assert "Goodbye!" in capsys.readouterr().out

    # KeyboardInterrupt
    repl_inst.session.prompt.side_effect = [KeyboardInterrupt(), "exit()"]
    repl_inst.run()

    # Generic Exception
    repl_inst.session.prompt.side_effect = [Exception("ouch"), "exit()"]
    repl_inst.run()
    assert "REPL Error: ouch" in capsys.readouterr().out


def test_repl_run_executes_code(mocker):
    runtime = MagicMock()
    repl_inst = ColabREPL(runtime)
    repl_inst.session = MagicMock()

    with patch.object(repl_inst, "execute") as mock_execute:
        repl_inst.session.prompt.side_effect = ["print(1)", "/quit"]
        repl_inst.run()
        mock_execute.assert_called_once_with("print(1)")


def test_repl_execute_with_history(mock_store):
    runtime = MagicMock()
    history_logger = MagicMock()
    repl_inst = ColabREPL(runtime, session_name="s1", history_logger=history_logger)

    repl_inst.execute("print(1)")
    assert history_logger.log_event.called


def test_repl_key_bindings(mocker):
    runtime = MagicMock()
    repl_inst = ColabREPL(runtime)

    # Trigger 'enter' binding (which is mapped to c-m in prompt_toolkit)
    mock_event = MagicMock()
    repl_inst.kb.get_bindings_for_keys(("c-m",))[0].handler(mock_event)
    assert mock_event.current_buffer.validate_and_handle.called

    # Trigger 'c-j' binding
    mock_event = MagicMock()
    repl_inst.kb.get_bindings_for_keys(("c-j",))[0].handler(mock_event)
    mock_event.current_buffer.insert_text.assert_called_with("\n")
