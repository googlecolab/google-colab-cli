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

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import typer
from colab_cli.client import ColabRequestError
from colab_cli.state import SessionState
from colab_cli.commands.session import new, stop, keep_alive


def test_session_state_with_pid():
    s = SessionState(
        name="test",
        token="tok",
        url="http://",
        endpoint="end",
        keep_alive_pid=1234,
    )
    data = s.model_dump()
    assert data["keep_alive_pid"] == 1234

    s2 = SessionState(**data)
    assert s2.keep_alive_pid == 1234


@patch("colab_cli.commands.session.spawn_keep_alive")
def test_new_spawns_keep_alive(mock_spawn, mock_common_state):
    # mock_common_state is automatically provided by conftest.py
    mock_common_state.client.assign.return_value = MagicMock(
        endpoint="e1", runtime_proxy_info=MagicMock(token="t1", url="u1")
    )
    mock_spawn.return_value = 9999

    new(session="test-sess")

    assert mock_spawn.called
    # Endpoint and session name are positional; auth provider is propagated
    # so the detached daemon uses the same provider as the parent (otherwise
    # the daemon falls back to Typer's --auth=loas2 default and silently
    # uses the wrong auth backend — verified live 2026-04-30).
    assert mock_spawn.call_args.args == ("e1", "test-sess")
    assert "auth_provider" in mock_spawn.call_args.kwargs

    # Verify PID is saved in state
    assert mock_common_state.store.add.called
    state_saved = mock_common_state.store.add.call_args[0][0]
    assert state_saved.keep_alive_pid == 9999


def test_spawn_keep_alive_command_includes_auth_flag(mocker):
    """spawn_keep_alive() must propagate `--auth=<provider>` as a global flag
    BEFORE the `keep-alive` subcommand name. Without this, the detached child
    falls back to the Typer default (loas2). Pin the exact arg ordering.
    """
    from colab_cli.auth import AuthProvider
    from colab_cli.commands.session import spawn_keep_alive

    mock_popen = mocker.patch("colab_cli.commands.session.subprocess.Popen")
    mock_popen.return_value.pid = 12345

    spawn_keep_alive("ep1", "sess1", auth_provider=AuthProvider.ADC)

    cmd = mock_popen.call_args.args[0]
    # Global flags must come before the subcommand name in Typer.
    assert "--auth=adc" in cmd
    auth_idx = cmd.index("--auth=adc")
    keep_alive_idx = cmd.index("keep-alive")
    assert auth_idx < keep_alive_idx, f"--auth must precede 'keep-alive' but got: {cmd}"
    # Endpoint and session_name must follow `keep-alive` in order.
    assert cmd[keep_alive_idx + 1] == "ep1"
    assert cmd[keep_alive_idx + 2] == "sess1"


def test_spawn_keep_alive_command_includes_config_path(mocker):
    """spawn_keep_alive() must propagate `--config <path>` as a global flag
    so the daemon reads the same session state file as the parent. Without
    this, a parent invoked with `--config /tmp/foo/sessions.json` writes
    there but the daemon reads the default `~/.config/colab-cli/sessions.json`,
    finds no session, and exits with `reason=session_not_found`. Discovered
    while running the soak integration test 2026-04-30.
    """
    from colab_cli.commands.session import spawn_keep_alive

    mock_popen = mocker.patch("colab_cli.commands.session.subprocess.Popen")
    mock_popen.return_value.pid = 12345

    spawn_keep_alive("ep1", "sess1", config_path="/tmp/test/sessions.json")

    cmd = mock_popen.call_args.args[0]
    assert "--config" in cmd
    cfg_idx = cmd.index("--config")
    assert cmd[cfg_idx + 1] == "/tmp/test/sessions.json"
    keep_alive_idx = cmd.index("keep-alive")
    assert cfg_idx < keep_alive_idx, (
        f"--config must precede 'keep-alive' but got: {cmd}"
    )


def test_spawn_keep_alive_omits_optional_flags_when_none(mocker):
    """Backwards compat: callers that don't pass optional global flags get a
    command line without them (the daemon uses Typer defaults)."""
    from colab_cli.commands.session import spawn_keep_alive

    mock_popen = mocker.patch("colab_cli.commands.session.subprocess.Popen")
    mock_popen.return_value.pid = 12345

    spawn_keep_alive("ep1", "sess1")

    cmd = mock_popen.call_args.args[0]
    assert not any(c.startswith("--auth") for c in cmd)
    assert "--config" not in cmd


@patch("colab_cli.commands.session.spawn_keep_alive")
def test_new_skips_keep_alive_under_loas2(mock_spawn, mock_common_state):
    """Under --auth=loas2, the keep-alive daemon must NOT be spawned and the
    pre-flight ping must NOT be made. The corp LOAS-to-OAuth exchange policy
    does not allowlist the `colaboratory` scope (see auth.py), so any
    keep_alive_assignment call would fail with 403 SCOPE_NOT_PERMITTED. Until
    the policy exception lands (go/enterprise-identity-intake), spawning the
    daemon is just busywork that ends in a guaranteed failure.
    """
    from colab_cli.auth import AuthProvider

    mock_common_state.auth_provider = AuthProvider.LOAS2
    mock_common_state.client.assign.return_value = MagicMock(
        endpoint="e1", runtime_proxy_info=MagicMock(token="t1", url="u1")
    )

    new(session="loas2-sess")

    # Session is still persisted (the user wants the VM).
    mock_common_state.store.add.assert_called_once()
    state_saved = mock_common_state.store.add.call_args[0][0]
    # ...but with no keep-alive PID — explicit signal to `colab stop` and
    # status commands that no daemon was spawned.
    assert state_saved.keep_alive_pid is None

    # Crucially, the daemon was not spawned and no pre-flight ping was made.
    mock_spawn.assert_not_called()
    mock_common_state.client.keep_alive_assignment.assert_not_called()


@patch("colab_cli.commands.session.spawn_keep_alive")
def test_new_runs_keep_alive_preflight(mock_spawn, mock_common_state):
    """`colab new` should pre-flight the keep-alive RPC before persisting the
    session, so missing-scope failures are surfaced immediately rather than
    silently after ~2 minutes."""
    mock_common_state.client.assign.return_value = MagicMock(
        endpoint="e1", runtime_proxy_info=MagicMock(token="t1", url="u1")
    )
    mock_spawn.return_value = 9999

    new(session="test-sess")

    mock_common_state.client.keep_alive_assignment.assert_called_once_with("e1")


@patch("colab_cli.commands.session.spawn_keep_alive")
def test_new_aborts_on_missing_scope(mock_spawn, mock_common_state):
    """A 403 SCOPE_NOT_PERMITTED on pre-flight should:
    - print actionable remediation,
    - unassign the VM (so we don't leak a billable assignment),
    - exit non-zero,
    - and NOT spawn the keep-alive daemon or persist the session.
    """
    mock_common_state.client.assign.return_value = MagicMock(
        endpoint="e1", runtime_proxy_info=MagicMock(token="t1", url="u1")
    )
    mock_response = MagicMock()
    mock_response.status_code = 403
    scope_error = ColabRequestError(
        "Forbidden",
        MagicMock(),
        mock_response,
        response_body=(
            '[7,"Request had insufficient authentication scopes.",[["type.'
            'googleapis.com/google.rpc.DebugInfo",[null,"Authentication error: '
            "2; Error Details: {AuthType:7,ErrorCode:2,DebugInfo:gaia_mint_"
            'exchange::SCOPE_NOT_PERMITTED}"]]]]'
        ),
    )
    mock_common_state.client.keep_alive_assignment.side_effect = scope_error

    with pytest.raises(typer.Exit) as excinfo:
        new(session="test-sess")
    assert excinfo.value.exit_code == 1

    # We unassigned the VM we just created.
    mock_common_state.client.unassign.assert_called_once_with("e1")
    # We did NOT spawn the keep-alive daemon.
    mock_spawn.assert_not_called()
    # We did NOT persist the session.
    mock_common_state.store.add.assert_not_called()


@patch("colab_cli.commands.session.spawn_keep_alive")
def test_new_tolerates_non_scope_preflight_error(mock_spawn, mock_common_state):
    """Non-scope errors (e.g. transient 5xx, 400 from a different cause) on
    pre-flight should NOT block session creation — the daemon will retry and
    log via the existing keep_alive_error path.
    """
    mock_common_state.client.assign.return_value = MagicMock(
        endpoint="e1", runtime_proxy_info=MagicMock(token="t1", url="u1")
    )
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_common_state.client.keep_alive_assignment.side_effect = ColabRequestError(
        "Service Unavailable",
        MagicMock(),
        mock_response,
        response_body="upstream timeout",
    )
    mock_spawn.return_value = 9999

    new(session="test-sess")

    # We did NOT unassign — the session is still usable.
    mock_common_state.client.unassign.assert_not_called()
    # Daemon spawned and session persisted. `store.add` is called twice in
    # the daemon-spawning path: once BEFORE spawn (so the daemon's initial
    # session-existence check doesn't race), and once AFTER to record the
    # keep_alive_pid. Final state must include the PID.
    mock_spawn.assert_called_once()
    assert mock_common_state.store.add.call_count == 2
    final_state = mock_common_state.store.add.call_args.args[0]
    assert final_state.keep_alive_pid == 9999


@patch("colab_cli.common.kill_process")
def test_stop_kills_keep_alive(mock_kill, mock_common_state):
    mock_common_state.resolve_session.return_value = "test-sess"
    mock_common_state.store.get.return_value = SessionState(
        name="test-sess", token="t1", url="u1", endpoint="e1", keep_alive_pid=9999
    )

    stop(session="test-sess")

    mock_kill.assert_called_once_with(9999)


def test_keep_alive_loop_basic(mock_common_state):
    mock_common_state.store.get.return_value = SessionState(
        name="test", token="t", url="u", endpoint="e1"
    )

    with (
        patch("time.sleep", side_effect=InterruptedError),
        patch("time.time", side_effect=[0, 100]),
    ):
        with pytest.raises(InterruptedError):
            keep_alive("e1", "test")

    mock_common_state.client.keep_alive_assignment.assert_called_once_with("e1")


def test_keep_alive_exits_on_consecutive_4xx(mock_common_state):
    # Mock response for 404 error
    mock_response = MagicMock()
    mock_response.status_code = 404
    error = ColabRequestError("Not Found", MagicMock(), mock_response)

    mock_common_state.store.get.return_value = SessionState(
        name="test", token="t", url="u", endpoint="e1"
    )
    mock_common_state.client.keep_alive_assignment.side_effect = error

    with (
        patch("time.sleep") as mock_sleep,
        patch("time.time", side_effect=range(0, 10000, 60)),
    ):
        # It should exit after 2 calls to ping (consecutive 4xx)
        # We'll use side_effect on mock_sleep to detect if it loops too much
        mock_sleep.side_effect = [None, None, Exception("LoopTooLong")]

        try:
            keep_alive("e1", "test")
        except Exception as e:
            if str(e) == "LoopTooLong":
                pytest.fail("Keep alive loop did not exit after consecutive 4xx")
            raise

        assert mock_common_state.client.keep_alive_assignment.call_count == 2


def test_keep_alive_resets_on_success(mock_common_state):
    # Mock response for 404 error
    mock_response_404 = MagicMock()
    mock_response_404.status_code = 404
    error_404 = ColabRequestError("Not Found", MagicMock(), mock_response_404)

    mock_common_state.store.get.return_value = SessionState(
        name="test", token="t", url="u", endpoint="e1"
    )

    # ping sequence: 404, success, 404, 404
    mock_common_state.client.keep_alive_assignment.side_effect = [
        error_404,
        None,
        error_404,
        error_404,
    ]

    with (
        patch("time.sleep") as mock_sleep,
        patch("time.time", side_effect=range(0, 10000, 60)),
    ):
        # We need to make sure it doesn't loop forever
        mock_sleep.side_effect = [None, None, None, Exception("StopLoop")]

        try:
            keep_alive("e1", "test")
        except Exception as e:
            if str(e) != "StopLoop":
                raise


@patch("colab_cli.common.kill_process")
def test_sync_sessions_handles_lost_vm(mock_kill, mock_common_state):
    # Server returns empty list (indicating VM is gone)
    mock_common_state.client.list_assignments.return_value = []

    lost_session = SessionState(
        name="lost-sess", token="t1", url="u1", endpoint="e1", keep_alive_pid=7777
    )
    # Local session has a keep_alive_pid
    mock_common_state.store.list.return_value = {"lost-sess": lost_session}
    # Ensure store.get returns the session too
    mock_common_state.store.get.return_value = lost_session

    from colab_cli.common import State

    real_state = State()

    with (
        patch.object(State, "store", new_callable=PropertyMock) as mock_store_prop,
        patch.object(State, "client", new_callable=PropertyMock) as mock_client_prop,
        patch.object(State, "history", new_callable=PropertyMock) as mock_hist_prop,
    ):
        mock_store_prop.return_value = mock_common_state.store
        mock_client_prop.return_value = mock_common_state.client
        mock_hist_prop.return_value = mock_common_state.history

        real_state.sync_sessions()

    mock_kill.assert_called_with(7777)


def test_keep_alive_logging(mock_common_state):
    # Mock successful run that eventually hits time limit
    mock_common_state.store.get.return_value = SessionState(
        name="test", token="t", url="u", endpoint="e1"
    )

    # time.time() is called: start_time, loop-condition (force exit),
    # and once at the end for duration_seconds calculation.
    with (
        patch("time.time", side_effect=[0, 24 * 3600 + 1, 24 * 3600 + 1]),
        patch("time.sleep"),
    ):
        keep_alive("e1", "test")

    # Verify logging
    log_calls = mock_common_state.history.log_event.call_args_list
    started = [c for c in log_calls if c.args[1] == "keep_alive_started"]
    assert started, "expected keep_alive_started event"
    assert started[0].args[2]["endpoint"] == "e1"
    assert "pid" in started[0].args[2]

    stopped = [c for c in log_calls if c.args[1] == "keep_alive_stopped"]
    assert stopped, "expected keep_alive_stopped event"
    payload = stopped[0].args[2]
    assert payload["reason"] == "time_limit_reached"
    assert "iterations" in payload
    assert "duration_seconds" in payload


def test_keep_alive_logging_session_gone(mock_common_state):
    # Session not found in store
    mock_common_state.store.get.return_value = None

    with patch("time.time", return_value=0), patch("time.sleep"):
        keep_alive("e1", "test")

    log_calls = mock_common_state.history.log_event.call_args_list
    stopped = [c for c in log_calls if c.args[1] == "keep_alive_stopped"]
    assert stopped, "expected keep_alive_stopped event"
    payload = stopped[0].args[2]
    assert payload["reason"] == "session_not_found"
    assert payload["iterations"] == 1


def test_keep_alive_logs_endpoint_mismatch_details(mock_common_state):
    # Session exists but endpoint has changed.
    mock_common_state.store.get.return_value = SessionState(
        name="test", token="t", url="u", endpoint="e2-new"
    )

    with patch("time.time", return_value=0), patch("time.sleep"):
        keep_alive("e1-old", "test")

    log_calls = mock_common_state.history.log_event.call_args_list
    stopped = [c for c in log_calls if c.args[1] == "keep_alive_stopped"]
    assert stopped, "expected keep_alive_stopped event"
    payload = stopped[0].args[2]
    assert payload["reason"] == "endpoint_mismatch"
    assert payload["expected_endpoint"] == "e1-old"
    assert payload["actual_endpoint"] == "e2-new"


def test_keep_alive_logs_error_events_and_last_error(mock_common_state):
    # Two consecutive 4xx errors -> exit, with per-error events + last_error in stop.
    mock_response = MagicMock()
    mock_response.status_code = 404
    error = ColabRequestError("Not Found", MagicMock(), mock_response)

    mock_common_state.store.get.return_value = SessionState(
        name="test", token="t", url="u", endpoint="e1"
    )
    mock_common_state.client.keep_alive_assignment.side_effect = error

    with (
        patch("time.sleep"),
        patch("time.time", side_effect=range(0, 10000, 60)),
    ):
        keep_alive("e1", "test")

    log_calls = mock_common_state.history.log_event.call_args_list

    errors = [c for c in log_calls if c.args[1] == "keep_alive_error"]
    assert len(errors) == 2, "expected one keep_alive_error per failed ping"
    assert errors[0].args[2]["status_code"] == 404
    assert errors[0].args[2]["error_type"] == "ColabRequestError"

    stopped = [c for c in log_calls if c.args[1] == "keep_alive_stopped"]
    assert stopped, "expected keep_alive_stopped event"
    payload = stopped[0].args[2]
    assert payload["reason"] == "consecutive_4xx_errors"
    assert payload["last_error"]["status_code"] == 404
    assert payload["last_error"]["error_type"] == "ColabRequestError"
