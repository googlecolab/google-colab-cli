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

"""Recovery must keep the original request across credential and process loss."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from mock_kernel import (
    MockKernelClient,
    connect_runtime,
    reply_message,
    status_message,
    stdout_message,
)
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.common import State
from colab_cli.execution_journal import ExecutionJournal
from colab_cli.runtime import ColabRuntime, ExecutionOutcomeUnknown
from colab_cli.state import SessionState, StateStore

PATH = "/content/.colab-cli-executions/" + "a" * 32 + ".jsonl"


def test_refresh_updates_original_session_without_allocating(tmp_path):
    state = State()
    state._store = StateStore(str(tmp_path / "sessions.json"))
    state._client = Mock()
    session = SessionState(
        name="s",
        token="old",
        url="https://old",
        endpoint="original",
        kernel_id="kernel",
        session_id="jupyter",
    )
    state.store.add(session)
    from colab_cli.client import RuntimeProxyInfo

    state.client.list_assignments.return_value = [
        SimpleNamespace(
            endpoint="original",
            runtime_proxy_info=RuntimeProxyInfo.model_validate(
                {
                    "token": "fresh",
                    "url": "https://fresh",
                    "tokenExpiresInSeconds": 3600,
                }
            ),
        )
    ]
    assert state.refresh_runtime_proxy(session, timeout=2) == ("https://fresh", "fresh")
    assert (session.kernel_id, session.session_id) == ("kernel", "jupyter")
    assert state.store.get("s").token == "fresh"
    state.client.list_assignments.assert_called_once_with(timeout=2)
    state.client.assign.assert_not_called()
    state.client.unassign.assert_not_called()


def test_refresh_never_substitutes_another_assignment(tmp_path):
    state = State()
    state._store = StateStore(str(tmp_path / "sessions.json"))
    state._client = Mock()
    session = SessionState(name="s", token="old", url="https://old", endpoint="ours")
    state.store.add(session)
    state.client.list_assignments.return_value = [SimpleNamespace(endpoint="other")]
    with pytest.raises(RuntimeError, match="original.*assignment|Original.*assignment"):
        state.refresh_runtime_proxy(session, timeout=2)
    assert state.store.get("s").endpoint == "ours"
    state.client.assign.assert_not_called()


def test_journal_can_reopen_persisted_request_on_a_fresh_proxy(mocker):
    journal = ExecutionJournal("https://old", "old", "req-1", path=PATH)
    journal.update_connection("https://fresh", "fresh")
    response = Mock()
    response.json.return_value = {"format": "text", "content": ""}
    get = mocker.patch(
        "colab_cli.execution_journal.requests.get", return_value=response
    )
    assert journal.read(timeout=2) == []
    assert get.call_args.args[0].startswith("https://fresh/api/contents/")
    assert get.call_args.kwargs["params"]["colab-runtime-proxy-token"] == "fresh"
    assert journal.path == PATH


def test_recovery_refreshes_a_rejected_proxy_without_resubmitting(monkeypatch):
    refreshed = Mock(return_value=("https://fresh", "fresh"))
    runtime = ColabRuntime("https://old", "old", refresh_connection=refreshed)
    client = MockKernelClient([])
    client.kernel_socket.sock = None
    client.connection_thread = SimpleNamespace(is_alive=lambda: False)
    connect_runtime(runtime, client, monkeypatch)
    journal = Mock()
    response = Mock(status_code=403)
    journal.read.side_effect = [
        requests.HTTPError(response=response),
        [stdout_message("original output\n"), reply_message(), status_message("idle")],
    ]
    monkeypatch.setattr(runtime, "_start_execution", lambda *a: ("req-1", journal))
    outputs = runtime.execute_code("side_effect()", timeout=5)
    assert outputs[0]["text"] == "original output\n"
    refreshed.assert_called_once()
    journal.update_connection.assert_called_with("https://fresh", "fresh")
    assert runtime.url == "https://fresh"
    assert not hasattr(client, "code"), "recovery must not call execute"


def test_readable_journal_keeps_tracking_after_websocket_thread_stops(monkeypatch):
    runtime = ColabRuntime("https://runtime", "token")
    client = MockKernelClient([])
    client.kernel_socket.sock = None
    client.connection_thread = SimpleNamespace(is_alive=lambda: False)
    connect_runtime(runtime, client, monkeypatch)
    journal = Mock()
    journal.read.side_effect = lambda **kw: (
        [stdout_message("finished\n"), reply_message(), status_message("idle")]
        if client.now >= 40
        else []
    )
    monkeypatch.setattr(runtime, "_start_execution", lambda *a: ("req-1", journal))
    outputs = runtime.execute_code("side_effect()", timeout=0)
    assert outputs[0]["text"] == "finished\n"
    assert client.now >= 40
    assert runtime._restart_transport, (
        "The next cell needs a fresh socket on the same kernel"
    )


def test_recovery_record_is_saved_before_user_code_is_sent(mocker):
    events = []
    runtime = ColabRuntime(
        "https://runtime", "token", on_execution_record=lambda r: events.append(r)
    )
    client = Mock()
    client.session.msg.return_value = {"header": {"msg_id": "original"}}
    mocker.patch.object(
        runtime, "_wait_for_execution", return_value={"content": {"status": "ok"}}
    )
    client.shell_channel.send.side_effect = lambda request: events.append("submitted")
    request, journal = runtime._start_execution(client, "side_effect()")
    assert request == "original"
    assert events == [{"request_id": "original", "path": journal.path}, "submitted"]


def test_resume_uses_records_without_starting_a_kernel_or_sending_code(mocker):
    runtime = ColabRuntime("https://runtime", "token")
    client_class = mocker.patch("colab_cli.runtime.jupyter_kernel_client.KernelClient")
    read = mocker.patch(
        "colab_cli.execution_journal.ExecutionJournal.read",
        return_value=[
            stdout_message("saved result\n"),
            reply_message(),
            status_message("idle"),
        ],
    )
    mocker.patch("colab_cli.execution_journal.ExecutionJournal.remove")
    outputs = runtime.resume_execution("req-1", PATH, timeout=2)
    assert outputs[0]["text"] == "saved result\n"
    client_class.assert_not_called()
    read.assert_called_once()


def test_session_store_retains_execution_identity_across_processes(tmp_path):
    path = str(tmp_path / "sessions.json")
    session = SessionState(
        name="s", token="token", url="https://runtime", endpoint="ours"
    )
    session.execution = {"request_id": "req-1", "path": PATH}
    StateStore(path).add(session)
    assert StateStore(path).get("s").execution == session.execution


def test_resume_command_never_submits_original_code(mock_common_state, mocker):
    session = SessionState(
        name="s",
        token="token",
        url="https://runtime",
        endpoint="ours",
        running="unknown: exec(stdin)",
    )
    session.execution = {"request_id": "req-1", "path": PATH}
    mock_common_state.resolve_session.return_value = "s"
    mock_common_state.store.get.return_value = session
    runtime = mocker.patch("colab_cli.commands.execution.ColabRuntime").return_value
    runtime.resume_execution.return_value = [
        {"output_type": "stream", "text": "recovered"}
    ]
    result = CliRunner().invoke(app, ["resume", "-s", "s"])
    assert result.exit_code == 0, result.output
    runtime.execute_code.assert_not_called()
    assert runtime.resume_execution.call_args.args == ("req-1", PATH)
    assert session.execution is None
    assert session.running is None
    mock_common_state.client.unassign.assert_not_called()


def test_resume_failure_keeps_record_for_another_attempt(mock_common_state, mocker):
    session = SessionState(
        name="s", token="token", url="https://runtime", endpoint="ours"
    )
    session.execution = {"request_id": "req-1", "path": PATH}
    mock_common_state.resolve_session.return_value = "s"
    mock_common_state.store.get.return_value = session
    runtime = mocker.patch("colab_cli.commands.execution.ColabRuntime").return_value
    runtime.resume_execution.side_effect = ExecutionOutcomeUnknown("still unavailable")
    result = CliRunner().invoke(app, ["resume", "-s", "s"])
    assert result.exit_code != 0
    assert session.execution["request_id"] == "req-1"
    mock_common_state.client.unassign.assert_not_called()


@pytest.mark.parametrize("command", ["exec", "run"])
def test_unresolved_work_cannot_be_overwritten(
    command, mock_common_state, mocker, tmp_path
):
    session = SessionState(
        name="s",
        token="t",
        url="https://runtime",
        endpoint="ours",
        execution={"request_id": "req-1", "path": PATH},
    )
    mock_common_state.resolve_session.return_value = "s"
    mock_common_state.store.get.return_value = session
    runtime = mocker.patch(
        f"colab_cli.commands.{'execution' if command == 'exec' else 'run'}.ColabRuntime"
    )
    script = tmp_path / "work.py"
    script.write_text("side_effect()")
    args = [command, "-s", "s"] + ([str(script)] if command == "run" else [])
    result = CliRunner().invoke(app, args, input="side_effect()")
    assert result.exit_code == 1
    assert "colab resume -s s" in result.output
    runtime.assert_not_called()
    mock_common_state.client.assign.assert_not_called()
    assert session.execution["request_id"] == "req-1"


def test_missing_completion_on_idle_kernel_is_bounded_without_idle_timeout(
    monkeypatch, mocker
):
    runtime = ColabRuntime("https://runtime", "t", kernel_id="original")
    client = MockKernelClient([])
    client.kernel_socket.sock = None
    connect_runtime(runtime, client, monkeypatch)
    journal = Mock()
    journal.read.return_value = []
    response = Mock()
    response.json.return_value = {"execution_state": "idle"}
    mocker.patch("colab_cli.runtime.requests.get", return_value=response)
    monkeypatch.setattr(runtime, "_start_execution", lambda *a: ("req-1", journal))
    with pytest.raises(ExecutionOutcomeUnknown, match="completion record is missing"):
        runtime.execute_code("side_effect()", timeout=0)
    assert 30 <= client.now <= 35
    journal.remove.assert_not_called()


def test_failure_of_both_transports_is_bounded_and_does_not_clear_record(monkeypatch):
    saved = Mock()
    refresh = Mock(side_effect=requests.ConnectionError())
    runtime = ColabRuntime(
        "https://runtime", "t", refresh_connection=refresh, on_execution_record=saved
    )
    client = MockKernelClient([])
    client.kernel_socket.sock = None
    connect_runtime(runtime, client, monkeypatch)
    journal = Mock()
    journal.read.side_effect = requests.ConnectionError()
    monkeypatch.setattr(runtime, "_start_execution", lambda *a: ("req-1", journal))
    with pytest.raises(ExecutionOutcomeUnknown):
        runtime.execute_code("side_effect()", timeout=0)
    assert 30 <= client.now <= 35
    assert refresh.call_count <= 7
    saved.assert_not_called()
    journal.remove.assert_not_called()


def test_saved_identity_survives_uncertain_submission(mocker):
    saved = []
    runtime = ColabRuntime("https://runtime", "t", on_execution_record=saved.append)
    client = Mock()
    client.session.msg.return_value = {"header": {"msg_id": "original"}}
    client.shell_channel.send.side_effect = OSError("disconnected during send")
    mocker.patch.object(
        runtime, "_wait_for_execution", return_value={"content": {"status": "ok"}}
    )
    with pytest.raises(ExecutionOutcomeUnknown):
        runtime._start_execution(client, "side_effect()")
    assert len(saved) == 1 and saved[0]["request_id"] == "original"
    client.shell_channel.send.assert_called_once()


def test_missing_assignment_does_not_prune_pending_execution(tmp_path):
    from datetime import UTC, datetime, timedelta

    state = State()
    state._store = StateStore(str(tmp_path / "sessions.json"))
    state._client = Mock()
    state.client.list_assignments.return_value = []
    session = SessionState(
        name="s",
        token="t",
        url="https://runtime",
        endpoint="ours",
        token_expires_at=datetime.now(UTC) - timedelta(hours=1),
        execution={"request_id": "req-1", "path": PATH},
    )
    state.store.add(session)
    assert state.get_session("s").execution == session.execution
    state.sync_sessions()
    assert state.store.get("s").execution == session.execution


def test_resume_does_not_report_aborted_work_as_success(mocker):
    runtime = ColabRuntime("https://runtime", "t")
    mocker.patch(
        "colab_cli.execution_journal.ExecutionJournal.read",
        return_value=[
            reply_message("aborted"),
            status_message("idle"),
        ],
    )
    mocker.patch("colab_cli.execution_journal.ExecutionJournal.remove")
    outputs = runtime.resume_execution("req-1", PATH)
    assert outputs[0]["output_type"] == "error"
    assert outputs[0]["ename"] == "ExecutionAborted"


def test_socket_rebuild_does_not_reuse_jupyter_message_ids(mocker):
    import jupyter_kernel_client
    from jupyter_client.session import Session

    runtime = ColabRuntime(
        "https://runtime", "t", kernel_id="original", session_id="same"
    )
    old = Mock()
    old._manager.client.session = Session(session="same")
    old._manager.client.session.message_count = 40
    runtime._kernel_client = old
    runtime._restart_transport = True
    name = next(
        name
        for name in ("ColabKernelClient", "KernelClient", "JupyterKernelClient")
        if hasattr(jupyter_kernel_client, name)
    )
    fresh = mocker.patch.object(jupyter_kernel_client, name).return_value
    fresh._manager.client.session = Session(session="same")
    mocker.patch.object(runtime, "_apply_ws_hook")
    assert runtime.kernel_client is fresh
    assert fresh._manager.client.session.message_count >= 40


def test_new_runtime_in_same_process_does_not_reuse_saved_session_message_ids(mocker):
    import jupyter_kernel_client
    from jupyter_client.session import Session

    clients = [Mock(), Mock()]
    for client in clients:
        client._manager.client.session = Session(session="same")
    name = next(
        name
        for name in ("ColabKernelClient", "KernelClient", "JupyterKernelClient")
        if hasattr(jupyter_kernel_client, name)
    )
    mocker.patch.object(jupyter_kernel_client, name, side_effect=clients)
    mocker.patch.object(ColabRuntime, "_apply_ws_hook")
    identities = []
    for _ in range(2):
        runtime = ColabRuntime(
            "https://runtime", "t", kernel_id="original", session_id="same"
        )
        session = runtime.kernel_client._manager.client.session
        identities.append(session.msg("execute_request")["header"]["msg_id"])
    assert identities[0] != identities[1]
