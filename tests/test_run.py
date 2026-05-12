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

"""Tests for `colab run <script.py> [args...]` — shebang-friendly one-shot
execution that bundles `colab new` + `colab exec` + `colab stop`.
"""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import (
    Accelerator,
    PostAssignmentResponse,
    Variant,
)

runner = CliRunner()


@pytest.fixture
def mock_client(mock_common_state):
    return mock_common_state.client


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@pytest.fixture
def mock_runtime_class(mocker):
    """Patch ColabRuntime in the run module specifically."""
    return mocker.patch("colab_cli.commands.run.ColabRuntime")


@pytest.fixture
def mock_spawn_keep_alive(mocker):
    """Don't actually spawn a daemon during tests."""
    return mocker.patch("colab_cli.commands.run.spawn_keep_alive", return_value=12345)


@pytest.fixture
def assign_response():
    """A minimal PostAssignmentResponse-shaped mock for client.assign."""
    res = MagicMock()
    res.__class__ = PostAssignmentResponse
    res.runtime_proxy_info.token = "tok"
    res.runtime_proxy_info.url = "http://runtime"
    res.endpoint = "ep-123"
    return res


@pytest.fixture
def script_path(tmp_path):
    p = tmp_path / "script.py"
    p.write_text("print('hello from script')\n")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_basic_flow(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """`colab run script.py` should: assign, exec, unassign."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    # Simulate the persisted SessionState being readable by the run command.
    persisted = {}

    def store_add(s):
        persisted["s"] = s

    def store_get(name):
        return persisted.get("s")

    mock_store.add.side_effect = store_add
    mock_store.get.side_effect = store_get

    result = runner.invoke(app, ["run", str(script_path)])

    assert result.exit_code == 0, result.output
    # Allocation happened
    mock_client.assign.assert_called_once()
    # Script body was executed (the prelude + body is one execute_code call)
    code_calls = [c.args[0] for c in mock_runtime.execute_code.call_args_list]
    assert any("hello from script" in code for code in code_calls), (
        f"Script body never sent to runtime. Calls: {code_calls}"
    )
    # Cleanup happened
    mock_client.unassign.assert_called_once_with("ep-123")


# ---------------------------------------------------------------------------
# --keep flag
# ---------------------------------------------------------------------------


def test_run_keep_skips_unassign(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """With `--keep`, the session must NOT be unassigned after the script
    finishes — the user wants to attach to it later."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(app, ["run", "--keep", str(script_path)])

    assert result.exit_code == 0, result.output
    mock_client.assign.assert_called_once()
    mock_client.unassign.assert_not_called()
    mock_store.remove.assert_not_called()


# ---------------------------------------------------------------------------
# argv passthrough
# ---------------------------------------------------------------------------


def test_run_passes_argv(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """Args after the script must be exposed as `sys.argv` inside the kernel."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(
        app, ["run", str(script_path), "alpha", "beta", "--flag-for-script"]
    )

    assert result.exit_code == 0, result.output
    code_calls = [c.args[0] for c in mock_runtime.execute_code.call_args_list]
    # The execute_code call that contains the script body must also set
    # sys.argv to mirror native python invocation.
    body_calls = [c for c in code_calls if "hello from script" in c]
    assert body_calls, f"Body never executed. Calls: {code_calls}"
    body = body_calls[0]
    assert "sys.argv" in body
    assert "'script.py'" in body
    assert "'alpha'" in body
    assert "'beta'" in body
    assert "'--flag-for-script'" in body


def test_run_sets_dunder_main(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """The script must run with __name__ == '__main__'."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code == 0, result.output
    code_calls = [c.args[0] for c in mock_runtime.execute_code.call_args_list]
    body = next(c for c in code_calls if "hello from script" in c)
    assert "__name__" in body and "'__main__'" in body


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_run_propagates_error_exit_code(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """If the kernel reports an error, the CLI must exit non-zero AND still
    unassign the VM (try/finally guarantee — AGENTS.md item 10)."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value

    def execute_with_error(code, output_hook=None, **kwargs):
        outputs = [
            {
                "output_type": "error",
                "ename": "ValueError",
                "evalue": "boom",
                "traceback": ["Traceback...\n", "ValueError: boom\n"],
            }
        ]
        if output_hook:
            for o in outputs:
                output_hook(o)
        return outputs

    mock_runtime.execute_code.side_effect = execute_with_error

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code != 0
    # Cleanup MUST happen even on script failure.
    mock_client.unassign.assert_called_once_with("ep-123")


def test_run_unassign_called_on_exception_during_execute(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """Even if `runtime.execute_code` raises (e.g. websocket dies), the VM
    must be released."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.side_effect = RuntimeError("websocket closed")

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(app, ["run", str(script_path)])
    assert result.exit_code != 0
    mock_client.unassign.assert_called_once_with("ep-123")


# ---------------------------------------------------------------------------
# Accelerator passthrough
# ---------------------------------------------------------------------------


def test_run_with_gpu_flag(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """`colab run --gpu T4 script.py` must request a T4 GPU."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(app, ["run", "--gpu", "T4", str(script_path)])
    assert result.exit_code == 0, result.output

    _, kwargs = mock_client.assign.call_args
    assert kwargs["variant"] is Variant.GPU
    assert kwargs["accelerator"] is Accelerator.T4


def test_run_with_tpu_flag(
    mock_client,
    mock_store,
    mock_runtime_class,
    mock_spawn_keep_alive,
    assign_response,
    script_path,
):
    """`colab run --tpu v5e1 script.py` must request a TPU."""
    mock_client.assign.return_value = assign_response
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    persisted = {}
    mock_store.add.side_effect = lambda s: persisted.setdefault("s", s)
    mock_store.get.side_effect = lambda name: persisted.get("s")

    result = runner.invoke(app, ["run", "--tpu", "v5e1", str(script_path)])
    assert result.exit_code == 0, result.output

    _, kwargs = mock_client.assign.call_args
    assert kwargs["variant"] is Variant.TPU
    assert kwargs["accelerator"] is Accelerator.V5E1


# ---------------------------------------------------------------------------
# Argument validation — fail FAST, before allocating a VM
# ---------------------------------------------------------------------------


def test_run_missing_script_errors(mock_client):
    """Typer should reject the invocation if no script path is given."""
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0
    mock_client.assign.assert_not_called()


def test_run_nonexistent_script_errors_before_assign(mock_client):
    """If the script doesn't exist locally, fail BEFORE allocating a VM —
    otherwise a typo would burn billable compute."""
    result = runner.invoke(app, ["run", "/no/such/file.py"])
    assert result.exit_code != 0
    mock_client.assign.assert_not_called()
