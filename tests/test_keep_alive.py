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
    assert mock_spawn.call_args[0] == ("e1", "test-sess")

    # Verify PID is saved in state
    assert mock_common_state.store.add.called
    state_saved = mock_common_state.store.add.call_args[0][0]
    assert state_saved.keep_alive_pid == 9999


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

    with patch("time.sleep", side_effect=InterruptedError), \
         patch("time.time", side_effect=[0, 100]):
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

    with patch("time.sleep") as mock_sleep, \
         patch("time.time", side_effect=range(0, 10000, 60)):
        
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
    mock_common_state.client.keep_alive_assignment.side_effect = [error_404, None, error_404, error_404]

    with patch("time.sleep") as mock_sleep, \
         patch("time.time", side_effect=range(0, 10000, 60)):
        
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
    mock_common_state.store.list.return_value = {
        "lost-sess": lost_session
    }
    # Ensure store.get returns the session too
    mock_common_state.store.get.return_value = lost_session

    from colab_cli.common import State
    real_state = State()
    
    with patch.object(State, 'store', new_callable=PropertyMock) as mock_store_prop, \
         patch.object(State, 'client', new_callable=PropertyMock) as mock_client_prop, \
         patch.object(State, 'history', new_callable=PropertyMock) as mock_hist_prop:
         
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

    # 2 calls to time.time (start, and then loop condition)
    # 0 -> start_time, 100 -> start_time + max_duration (force exit)
    with patch("time.time", side_effect=[0, 24 * 3600 + 1]), \
         patch("time.sleep"):
        keep_alive("e1", "test")

    # Verify logging
    log_calls = mock_common_state.history.log_event.call_args_list
    assert any(
        c.args[1] == "keep_alive_started" and c.args[0] == "test" for c in log_calls
    )
    assert any(
        c.args[1] == "keep_alive_stopped"
        and c.args[0] == "test"
        and c.args[2]["reason"] == "time_limit_reached"
        for c in log_calls
    )


def test_keep_alive_logging_session_gone(mock_common_state):
    # Session not found in store
    mock_common_state.store.get.return_value = None

    with patch("time.time", return_value=0), \
         patch("time.sleep"):
        keep_alive("e1", "test")

    log_calls = mock_common_state.history.log_event.call_args_list
    assert any(
        c.args[1] == "keep_alive_stopped"
        and c.args[2]["reason"] == "session_not_found"
        for c in log_calls
    )
