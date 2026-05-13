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

from unittest.mock import patch
import pytest
from typer.testing import CliRunner
from colab_cli.cli import app
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://test.url",
        endpoint="e1",
    )


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_auth(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Success"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    assert mock_session.last_execution[0] == "automation:auth"
    assert mock_session.last_execution[1] is None
    assert mock_session.last_execution[2] is not None
    mock_state.store.add.assert_called_with(mock_session)

    # Verify ColabRuntime was invoked with the correct code
    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "os.environ['USE_AUTH_EPHEM'] = '0'" in called_code
    assert "auth.authenticate_user()" in called_code


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_install(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Installed"}]

    result = runner.invoke(app, ["install", "-s", "test-session", "pandas", "numpy"])
    assert result.exit_code == 0
    assert mock_session.last_execution[0] == "automation:install"
    assert mock_session.last_execution[2] is not None
    mock_state.store.add.assert_called_with(mock_session)

    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "subprocess" in called_code
    assert "pip" in called_code
    assert "pandas" in called_code
    assert "numpy" in called_code


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_drivemount(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Mounted"}]

    result = runner.invoke(app, ["drivemount", "-s", "test-session", "/foo/bar"])
    assert result.exit_code == 0

    # Verify ColabRuntime was invoked with the correct code
    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "drive.mount('/foo/bar')" in called_code
    assert mock_runtime.colab_request_hook is not None
    # Drivemount waits for the user to OAuth in their browser; the kernel
    # goes silent during that wait and the default 10s execute() timeout
    # would raise TimeoutError mid-flow. Insist on a generous timeout
    # (>= 5 minutes) being forwarded to runtime.execute_code.
    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_auth_uses_long_timeout(mock_state, mock_runtime_class, mock_session):
    """`colab auth` walks the user through a paste-the-code flow that
    routinely takes >10s, so it must pass a generous timeout to
    runtime.execute_code or the call will TimeoutError mid-flow."""
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Authenticated"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300
