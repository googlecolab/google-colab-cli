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

from unittest.mock import MagicMock, mock_open, patch
import pytest
from typer.testing import CliRunner
from colab_cli.cli import app
from colab_cli.commands.automation import (
    _parse_propagation_response,
    drive_propagation_failure_message,
)
from colab_cli.state import SessionState

runner = CliRunner()


class _FakeResponse:
    """Minimal stand-in for the requests.Response returned by creds.request."""

    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


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
    mock_state.get_session.return_value = mock_session
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
    mock_state.get_session.return_value = mock_session
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
    mock_state.get_session.return_value = mock_session
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
    mock_state.get_session.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Authenticated"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300


def _drive_request(msg_id="m1"):
    return {
        "content": {"request": {"authType": "dfs_ephemeral"}},
        "metadata": {"colab_msg_id": msg_id},
    }


@pytest.mark.parametrize(
    "body",
    [
        '{"success": true, "token": "tok"}',
        'prefix\n{"success": true, "token": "tok"}',
        'rendered\nprefix\n{"success": true, "token": "tok"}',
    ],
)
def test_parse_propagation_response_reads_json(body):
    assert _parse_propagation_response(_FakeResponse(body))["token"] == "tok"


@pytest.mark.parametrize(
    "body",
    ["", "<html><body>400 Bad Request</body></html>", "not json", "null", "[1, 2]"],
)
def test_parse_propagation_response_never_raises(body):
    """Error bodies are often HTML; parsing must degrade to an empty dict."""
    assert _parse_propagation_response(_FakeResponse(body)) == {}


def test_drive_propagation_failure_message_is_actionable():
    message = drive_propagation_failure_message(400, {})

    assert "HTTP 400" in message
    assert "colab whoami" in message
    assert "different Google account" in message
    assert "<html" not in message


def test_drive_propagation_failure_message_includes_server_detail():
    message = drive_propagation_failure_message(403, {"error": "quota exceeded"})

    assert "Server said: quota exceeded" in message


def _invoke_drivemount_and_get_hook(mock_state, mock_runtime_class, mock_session):
    mock_state.get_session.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])
    assert result.exit_code == 0
    return mock_runtime.colab_request_hook


@patch("colab_cli.commands.automation.get_credentials")
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivefs_hook_reports_400_without_raw_html(
    mock_state, mock_runtime_class, mock_creds, mock_session, capsys
):
    """The multi-account 400 must not echo HTML or re-prompt for consent."""
    hook = _invoke_drivemount_and_get_hook(mock_state, mock_runtime_class, mock_session)
    mock_creds.return_value.request.side_effect = [
        _FakeResponse('prefix\n{"success": true, "token": "tok"}'),
        _FakeResponse(
            'prefix\n{"success": false, "unauthorized_redirect_uri": "https://auth"}'
        ),
        _FakeResponse("<html><body>400 Bad Request</body></html>", status_code=400),
    ]
    wsclient = MagicMock()
    wsclient.session.msg.return_value = {}

    with patch("builtins.open", mock_open(read_data="\n")):
        hook(_drive_request(), wsclient)

    out = capsys.readouterr().out
    assert "HTTP 400" in out
    assert "colab whoami" in out
    assert "<html" not in out
    # The kernel is unblocked instead of hanging until the 600s timeout.
    wsclient.stdin_channel.send.assert_called_once()
    assert any(
        call.args[1] == "drive_auth_failed"
        for call in mock_state.history.log_event.call_args_list
    )


@patch("colab_cli.commands.automation.get_credentials")
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivefs_hook_does_not_prompt_when_auth_is_not_the_cause(
    mock_state, mock_runtime_class, mock_creds, mock_session, capsys
):
    """A refusal without an auth URL must not claim Drive authorization is needed."""
    hook = _invoke_drivemount_and_get_hook(mock_state, mock_runtime_class, mock_session)
    mock_creds.return_value.request.side_effect = [
        _FakeResponse('prefix\n{"success": true, "token": "tok"}'),
        _FakeResponse('prefix\n{"success": false}'),
    ]
    wsclient = MagicMock()
    wsclient.session.msg.return_value = {}

    hook(_drive_request(), wsclient)

    out = capsys.readouterr().out
    assert "REQUIRED: Google Drive Authorization" not in out
    assert "could not be propagated" in out
    wsclient.stdin_channel.send.assert_called_once()


@patch("colab_cli.commands.automation.get_credentials")
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivefs_hook_reports_token_request_failure(
    mock_state, mock_runtime_class, mock_creds, mock_session, capsys
):
    hook = _invoke_drivemount_and_get_hook(mock_state, mock_runtime_class, mock_session)
    mock_creds.return_value.request.side_effect = [
        _FakeResponse("upstream error", status_code=500)
    ]
    wsclient = MagicMock()
    wsclient.session.msg.return_value = {}

    hook(_drive_request(), wsclient)

    out = capsys.readouterr().out
    assert "HTTP 500" in out
    wsclient.stdin_channel.send.assert_called_once()


@patch("colab_cli.commands.automation.get_credentials")
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivefs_hook_replies_after_successful_propagation(
    mock_state, mock_runtime_class, mock_creds, mock_session, capsys
):
    hook = _invoke_drivemount_and_get_hook(mock_state, mock_runtime_class, mock_session)
    mock_creds.return_value.request.side_effect = [
        _FakeResponse('prefix\n{"success": true, "token": "tok"}'),
        _FakeResponse(
            'prefix\n{"success": false, "unauthorized_redirect_uri": "https://auth"}'
        ),
        _FakeResponse('prefix\n{"success": true}'),
    ]
    wsclient = MagicMock()
    wsclient.session.msg.return_value = {}

    with patch("builtins.open", mock_open(read_data="\n")):
        hook(_drive_request(), wsclient)

    out = capsys.readouterr().out
    assert "Credentials propagated" in out
    wsclient.stdin_channel.send.assert_called_once()
