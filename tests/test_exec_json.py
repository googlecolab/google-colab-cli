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

"""Tests for `colab exec --json`, the machine-readable output mode."""

import base64
import json
from unittest.mock import MagicMock

import nbformat
import pytest
from nbformat.v4 import new_code_cell, new_notebook
from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()

SETUP_CODE = "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"


@pytest.fixture
def mock_runtime(mocker, mock_common_state):
    session = MagicMock()
    session.name = "s1"
    session.url = "http://url"
    session.token = "token"
    session.kernel_id = None
    session.session_id = None
    mock_common_state.store.get.return_value = session
    mock_common_state.resolve_session.return_value = "s1"
    runtime_class = mocker.patch("colab_cli.commands.execution.ColabRuntime")
    return runtime_class.return_value


def _script(runtime, per_code):
    """Make `execute_code` stream and return the outputs mapped to each code."""

    def execute_code(code, output_hook=None, **kwargs):
        if code == SETUP_CODE:
            return []
        outputs = per_code[code]
        if isinstance(outputs, Exception):
            raise outputs
        if output_hook:
            for out in outputs:
                output_hook(out)
        return outputs

    runtime.execute_code.side_effect = execute_code


def _records(stdout):
    return [json.loads(line) for line in stdout.splitlines()]


def test_exec_json_success_emits_one_record(mock_runtime):
    _script(
        mock_runtime,
        {
            "run()": [
                {"output_type": "stream", "name": "stdout", "text": "hello\n"},
                {"output_type": "stream", "name": "stderr", "text": "warn\n"},
                {
                    "output_type": "execute_result",
                    "data": {"text/plain": "42"},
                    "metadata": {},
                },
            ]
        },
    )

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="run()")

    assert result.exit_code == 0, result.output
    assert _records(result.stdout) == [
        {
            "cell_index": 0,
            "cell_id": None,
            "status": "ok",
            "stdout": "hello\n",
            "stderr": "warn\n",
            "data": [{"output_type": "execute_result", "text/plain": "42"}],
            "images": [],
            "error": None,
        }
    ]
    # The kernel's stderr is captured in the record, not replayed.
    assert result.stderr == ""


def test_exec_json_error_strips_ansi_and_exits_nonzero(mock_runtime):
    _script(
        mock_runtime,
        {
            "boom()": [
                {"output_type": "stream", "name": "stdout", "text": "before\n"},
                {
                    "output_type": "error",
                    "ename": "ValueError",
                    "evalue": "bad",
                    "traceback": [
                        "\x1b[0;31mValueError\x1b[0m Traceback",
                        "\x1b[1;32m----> 1\x1b[0m boom()",
                    ],
                },
            ]
        },
    )

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="boom()")

    assert result.exit_code == 1
    (record,) = _records(result.stdout)
    assert record["status"] == "error"
    assert record["stdout"] == "before\n"
    assert record["error"] == {
        "ename": "ValueError",
        "evalue": "bad",
        "traceback": "ValueError Traceback\n----> 1 boom()",
    }
    assert "\x1b" not in result.stdout
    assert result.stderr == ""


def test_exec_json_saves_images_and_reports_paths(mock_runtime, tmp_path):
    png = base64.b64encode(b"png-bytes").decode("ascii")
    _script(
        mock_runtime,
        {
            "plot()": [
                {
                    "output_type": "display_data",
                    "data": {"image/png": png, "text/plain": "<Figure>"},
                    "metadata": {},
                }
            ]
        },
    )
    target = tmp_path / "plot.png"

    result = runner.invoke(
        app,
        ["exec", "-s", "s1", "--json", "--output-image", str(target)],
        input="plot()",
    )

    assert result.exit_code == 0, result.output
    (record,) = _records(result.stdout)
    assert record["images"] == [str(target)]
    assert record["data"] == [{"output_type": "display_data", "text/plain": "<Figure>"}]
    assert png not in result.stdout
    assert target.read_bytes() == b"png-bytes"


def test_exec_json_notebook_emits_record_per_cell(mock_runtime, tmp_path):
    nb = new_notebook(
        cells=[
            new_code_cell("ok()", id="cell-a"),
            new_code_cell("fail()", id="cell-b"),
            new_code_cell("after()", id="cell-c"),
        ]
    )
    path = tmp_path / "nb.ipynb"
    nbformat.write(nb, str(path))
    _script(
        mock_runtime,
        {
            "ok()": [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            "fail()": [
                {
                    "output_type": "error",
                    "ename": "NameError",
                    "evalue": "fail",
                    "traceback": [],
                }
            ],
            "after()": [],
        },
    )

    result = runner.invoke(app, ["exec", "-s", "s1", "--json", "-f", str(path)])

    assert result.exit_code == 1
    records = _records(result.stdout)
    assert [(r["cell_index"], r["cell_id"], r["status"]) for r in records] == [
        (0, "cell-a", "ok"),
        (1, "cell-b", "error"),
        (2, "cell-c", "ok"),
    ]
    # Progress chatter moves to stderr so stdout stays pure NDJSON.
    assert "[colab] Executing cell 1/3" in result.stderr
    assert "_output.ipynb" in result.stderr


def test_exec_json_reports_client_side_failure(mock_runtime):
    _script(mock_runtime, {"slow()": TimeoutError("no output for 30s")})

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="slow()")

    assert result.exit_code == 1
    (record,) = _records(result.stdout)
    assert record["status"] == "error"
    assert record["error"] == {
        "ename": "TimeoutError",
        "evalue": "no output for 30s",
        "traceback": "",
    }


def test_exec_json_lost_session_keeps_stdout_clean(mock_runtime, mock_common_state):
    mock_runtime.execute_code.side_effect = Exception("404 Not Found")

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="print(1)")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "appears to be lost" in result.stderr
    mock_common_state.prune_session.assert_called_once_with("s1")


def test_exec_without_json_still_exits_zero_on_kernel_error(mock_runtime):
    """`--json` is opt-in: the default exit code and streams are unchanged."""
    _script(
        mock_runtime,
        {
            "boom()": [
                {
                    "output_type": "error",
                    "ename": "ValueError",
                    "evalue": "bad",
                    "traceback": [],
                }
            ]
        },
    )

    result = runner.invoke(app, ["exec", "-s", "s1"], input="boom()")

    assert result.exit_code == 0
    assert "ValueError: bad" in result.stderr
