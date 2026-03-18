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

import time
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import Assignment, PostAssignmentResponse

runner = CliRunner()


@pytest.fixture
def mock_client(mock_common_state):
    return mock_common_state.client


@pytest.fixture
def mock_store(mock_common_state):
    return mock_common_state.store


@pytest.fixture
def mock_history(mock_common_state):
    return mock_common_state.history


def test_cli_new_tpu(mock_client, mock_store):
    mock_res = MagicMock()
    mock_res.__class__ = PostAssignmentResponse
    mock_res.runtime_proxy_info.token = "t1"
    mock_res.runtime_proxy_info.url = "u1"
    mock_res.endpoint = "e1"
    mock_client.assign.return_value = mock_res

    result = runner.invoke(app, ["new", "-s", "my-session", "--tpu", "v5e1"])
    assert result.exit_code == 0

    added_state = mock_store.add.call_args[0][0]
    assert added_state.name == "my-session"
    assert added_state.variant == "TPU"
    assert added_state.accelerator == "V5E1"


def test_cli_new_gpu(mock_client, mock_store):
    mock_res = MagicMock()
    mock_res.__class__ = Assignment
    mock_res.runtime_proxy_token = "t2"
    mock_res.endpoint = "e2"
    del mock_res.runtime_proxy_info
    mock_client.assign.return_value = mock_res

    result = runner.invoke(app, ["new", "-s", "gpu-sess", "--gpu", "A100"])
    assert result.exit_code == 0

    added_state = mock_store.add.call_args[0][0]
    assert added_state.name == "gpu-sess"
    assert added_state.variant == "GPU"
    assert added_state.accelerator == "A100"
    assert added_state.token == "t2"


@pytest.mark.parametrize(
    "gpu_flag,expected_acc",
    [
        ("H100", "H100"),
        ("l4", "L4"),
        ("t4", "T4"),
        ("g4", "T4"),
    ],
)
def test_cli_new_gpu_variants(mock_client, mock_store, gpu_flag, expected_acc):
    mock_res = MagicMock()
    mock_res.__class__ = PostAssignmentResponse
    mock_res.runtime_proxy_info.token = "t1"
    mock_res.runtime_proxy_info.url = "u1"
    mock_res.endpoint = "e1"
    mock_client.assign.return_value = mock_res

    result = runner.invoke(app, ["new", "-s", "s", "--gpu", gpu_flag])
    assert result.exit_code == 0

    added_state = mock_store.add.call_args[0][0]
    assert added_state.accelerator == expected_acc


def test_cli_sessions(mock_client, mock_common_state):
    mock_assignment = MagicMock()
    mock_assignment.endpoint = "e1"
    mock_assignment.variant.value = "GPU"
    mock_assignment.accelerator.value = "NONE"

    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.endpoint = "e1"

    mock_common_state.sync_sessions.return_value = (
        {"s1": mock_session_state},
        [mock_assignment],
    )

    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "s1 -> e1" in result.output


def test_cli_status(mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.accelerator = "NONE"
    mock_session_state.variant = "DEFAULT"
    mock_session_state.last_execution = (
        "my_notebook.ipynb",
        "cell_1",
        "2023-10-27 12:00:00",
    )
    mock_store.get.return_value = mock_session_state

    mock_common_state.sync_sessions.return_value = ({"s1": mock_session_state}, [])

    # Test with explicit session
    result = runner.invoke(app, ["status", "-s", "s1"])
    assert result.exit_code == 0
    assert (
        "Last Execution: my_notebook.ipynb | Cell: cell_1 at 2023-10-27 12:00:00"
        in result.output
    )
    mock_store.get.assert_called_with("s1")

    # Test with missing session
    mock_store.get.return_value = None
    result = runner.invoke(app, ["status", "-s", "missing"])
    assert result.exit_code == 0
    assert "Session 'missing' not found" in result.output

    # Test list all sessions
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert (
        "Last Execution: my_notebook.ipynb | Cell: cell_1 at 2023-10-27 12:00:00"
        in result.output
    )

    # Test without execution metadata
    mock_session_state.last_execution = None
    mock_store.get.return_value = mock_session_state
    result = runner.invoke(app, ["status", "-s", "s1"])
    assert result.exit_code == 0
    assert "Last Execution" not in result.output


def test_cli_session_resolution(mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_session_state.name = "unique-session"
    mock_session_state.endpoint = "e1"
    mock_session_state.url = "http://url"
    mock_session_state.token = "token"
    mock_session_state.kernel_id = None

    # Setup for resolve_session
    mock_common_state.resolve_session.return_value = "unique-session"
    mock_store.get.return_value = mock_session_state

    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    mock_store.remove.assert_called_with("unique-session")


def test_cli_stop(mock_client, mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_session_state.endpoint = "e1"
    mock_session_state.name = "s1"
    mock_session_state.url = "http://url"
    mock_session_state.token = "token"
    mock_session_state.kernel_id = None
    mock_store.get.return_value = mock_session_state

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["stop", "-s", "s1"])
    assert result.exit_code == 0

    mock_client.unassign.assert_called_with("e1")
    mock_store.remove.assert_called_with("s1")


def test_cli_sessions_prune(mock_common_state):
    mock_assignment = MagicMock()
    mock_session_state1 = MagicMock()

    mock_common_state.sync_sessions.return_value = (
        {"s1": mock_session_state1},
        [mock_assignment],
    )
    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0


def test_cli_new_no_name(mock_client, mock_store):
    mock_res = MagicMock()
    mock_res.__class__ = PostAssignmentResponse
    mock_res.runtime_proxy_info.token = "t1"
    mock_res.runtime_proxy_info.url = "u1"
    mock_res.endpoint = "e1"
    mock_client.assign.return_value = mock_res

    result = runner.invoke(app, ["new"])
    assert result.exit_code == 0

    added_state = mock_store.add.call_args[0][0]
    assert len(added_state.name) == 6


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "Options" in result.output
    assert "Commands" in result.output


def test_cli_no_args():
    result = runner.invoke(app, [])
    # Typer with no_args_is_help=True might return 0 or 2 depending on version/config
    assert result.exit_code in [0, 2]


def test_cli_console(mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_session_state.name = "s1"
    mock_session_state.token = "t1"
    mock_session_state.url = "http://test.com"
    mock_store.get.return_value = mock_session_state

    mock_common_state.resolve_session.return_value = "s1"
    with patch("colab_cli.commands.execution.connect_console") as mock_connect:
        result = runner.invoke(app, ["console", "-s", "s1"])
        assert result.exit_code == 0
        mock_connect.assert_called_once_with(mock_session_state)


@patch("colab_cli.commands.files.ContentsClient")
def test_cli_ls(mock_contents_class, mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_store.get.return_value = mock_session_state

    mock_contents = mock_contents_class.return_value
    mock_contents.list_dir.return_value = {
        "type": "directory",
        "content": [
            {"name": "a_dir", "type": "directory"},
            {"name": "b_file", "type": "file"},
        ],
    }

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["ls", "-s", "s1", "content"])
    assert result.exit_code == 0

    assert "a_dir/" in result.output
    assert "b_file" in result.output


@patch("colab_cli.commands.files.ContentsClient")
def test_cli_rm(mock_contents_class, mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_store.get.return_value = mock_session_state

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["rm", "-s", "s1", "content/file.txt"])
    assert result.exit_code == 0

    mock_contents_class.return_value.rm.assert_called_once_with("content/file.txt")
    assert "Deleted content/file.txt" in result.output


@patch("colab_cli.commands.files.os.path.isfile")
@patch("colab_cli.commands.files.ContentsClient")
def test_cli_upload(mock_contents_class, mock_isfile, mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_store.get.return_value = mock_session_state
    mock_isfile.return_value = True

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["upload", "-s", "s1", "local.txt", "remote.txt"])
    assert result.exit_code == 0

    mock_contents_class.return_value.upload.assert_called_once_with(
        "local.txt", "remote.txt"
    )
    assert "Uploaded 'local.txt' to 'remote.txt'" in result.output


@patch("colab_cli.commands.files.ContentsClient")
def test_cli_download(mock_contents_class, mock_store, mock_common_state):
    mock_session_state = MagicMock()
    mock_store.get.return_value = mock_session_state

    mock_common_state.resolve_session.return_value = "s1"
    result = runner.invoke(app, ["download", "-s", "s1", "remote.txt", "local.txt"])
    assert result.exit_code == 0

    mock_contents_class.return_value.download.assert_called_once_with(
        "remote.txt", "local.txt"
    )
    assert "Downloaded 'remote.txt' to 'local.txt'" in result.output


@patch("colab_cli.commands.files.ContentsClient")
@patch("click.edit")
def test_cli_edit_no_changes(
    mock_edit, mock_contents_class, mock_store, mock_common_state
):
    mock_session_state = MagicMock()
    mock_store.get.return_value = mock_session_state

    mock_common_state.resolve_session.return_value = "s1"

    # Simulate editor making no changes by not modifying the file
    def mock_edit_side_effect(filename, **kwargs):
        pass

    mock_edit.side_effect = mock_edit_side_effect

    result = runner.invoke(app, ["edit", "-s", "s1", "remote.txt"])

    assert result.exit_code == 0
    mock_contents_class.return_value.download.assert_called_once()
    mock_contents_class.return_value.upload.assert_not_called()
    assert "No changes made to 'remote.txt'" in result.output


@patch("colab_cli.commands.files.ContentsClient")
@patch("click.edit")
def test_cli_edit_with_changes(
    mock_edit, mock_contents_class, mock_store, mock_common_state
):
    mock_session_state = MagicMock()
    mock_store.get.return_value = mock_session_state

    mock_common_state.resolve_session.return_value = "s1"

    # Simulate editor modifying the file
    def mock_edit_side_effect(filename, **kwargs):
        time.sleep(0.01)  # Ensure mtime differs if checking by mtime
        with open(filename, "a") as f:
            f.write("new content")

    mock_edit.side_effect = mock_edit_side_effect

    result = runner.invoke(app, ["edit", "-s", "s1", "remote.txt"])

    assert result.exit_code == 0
    mock_contents_class.return_value.download.assert_called_once()
    mock_contents_class.return_value.upload.assert_called_once()
    assert "Edited and uploaded 'remote.txt'" in result.output
