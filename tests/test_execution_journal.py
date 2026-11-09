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

"""Recovery records must survive a transport outage without changing execution."""

import json
from unittest.mock import Mock

import pytest
from jupyter_client.session import Session

from colab_cli.kernel_journal import install
from colab_cli.execution_journal import ExecutionJournal


def test_kernel_records_outputs_and_completion_before_delivery(tmp_path):
    session = Session()
    delivered = []
    path = tmp_path / "request.jsonl"

    def send(stream, msg, **kwargs):
        # A remote record must already exist when the transport sees a message.
        delivered.append(msg)
        assert (
            json.loads(path.read_text().splitlines()[-1])["header"]["msg_id"]
            == msg["header"]["msg_id"]
        )
        return msg

    session.send = send
    install(session, "original", str(path))
    parent = {"msg_id": "original", "session": "same-session"}
    for kind, content in (
        ("stream", {"name": "stdout", "text": "offline output"}),
        ("display_data", {"data": {"image/png": "aGVsbG8="}, "metadata": {}}),
        ("error", {"ename": "ValueError", "evalue": "failed", "traceback": []}),
        ("execute_reply", {"status": "error"}),
        ("status", {"execution_state": "idle"}),
    ):
        session.send(None, kind, content=content, parent=parent)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [msg["header"]["msg_type"] for msg in records] == [
        "stream",
        "display_data",
        "error",
        "execute_reply",
        "status",
    ]
    assert session.send is send  # Restore the kernel after completion.
    assert len(delivered) == 5


def test_kernel_does_not_capture_other_requests_or_swallow_delivery_errors(tmp_path):
    session = Session()
    original = Mock(side_effect=OSError("transport failed"))
    session.send = original
    path = tmp_path / "request.jsonl"
    install(session, "ours", str(path))
    with pytest.raises(OSError, match="transport failed"):
        session.send(None, "stream", {"text": "other"}, parent={"msg_id": "other"})
    assert path.read_text() == ""
    with pytest.raises(OSError, match="transport failed"):
        session.send(None, "stream", {"text": "ours"}, parent={"msg_id": "ours"})
    assert json.loads(path.read_text())["content"]["text"] == "ours"


def test_kernel_journal_failure_does_not_prevent_user_output(tmp_path):
    session = Session()
    original = Mock(side_effect=lambda stream, msg, **kw: msg)
    session.send = original
    path = tmp_path / "request.jsonl"
    install(session, "ours", str(path))
    path.unlink()
    path.mkdir()  # Simulate a journal that can no longer be written.
    msg = session.send(
        None, "stream", {"text": "still deliver"}, parent={"msg_id": "ours"}
    )
    assert msg["content"]["text"] == "still deliver"
    original.assert_called_once()


def test_journal_read_keeps_complete_messages_and_ignores_partial_tail(mocker):
    journal = ExecutionJournal("https://runtime", "secret", "req-1")
    msg = {
        "header": {"msg_type": "stream", "msg_id": "a"},
        "parent_header": {"msg_id": "req-1"},
        "content": {"text": "kept"},
        "_colab_cli_sequence": 1,
    }
    response = mocker.Mock()
    response.json.return_value = {
        "format": "text",
        "content": json.dumps(msg) + '\n{"header":',
    }
    request = mocker.patch(
        "colab_cli.execution_journal.requests.get", return_value=response
    )
    assert journal.read(timeout=2) == [msg]
    assert request.call_args.kwargs["timeout"] == 2
    assert request.call_args.kwargs["params"]["colab-runtime-proxy-token"] == "secret"
    # Polling the same snapshot must not replay previously consumed records.
    assert journal.read(timeout=2) == []


def test_journal_rejects_complete_corrupt_or_wrong_request_records(mocker):
    journal = ExecutionJournal("https://runtime", "secret", "req-1")
    response = mocker.Mock()
    request = mocker.patch(
        "colab_cli.execution_journal.requests.get", return_value=response
    )
    for content in ("not-json\n", '{"parent_header":{"msg_id":"other"}}\n'):
        response.json.return_value = {"format": "text", "content": content}
        with pytest.raises(ValueError):
            journal.read(timeout=2)
    assert request.call_count == 2


def test_start_execution_does_not_submit_user_code_without_journal_ack(mocker):
    from colab_cli.runtime import ColabRuntime

    runtime = ColabRuntime("https://runtime", "token")
    client = mocker.Mock()
    client.session.msg.return_value = {"header": {"msg_id": "original"}}
    mocker.patch.object(
        runtime,
        "_wait_for_execution",
        return_value={"content": {"status": "error", "evalue": "disk full"}},
    )
    with pytest.raises(RuntimeError, match="user code was not submitted"):
        runtime._start_execution(client, "side_effect()")
    client.shell_channel.send.assert_not_called()
    assert "side_effect()" not in client.execute.call_args.args[0]


def test_submission_failure_is_reported_as_unknown_without_retry(mocker):
    from colab_cli.runtime import ColabRuntime, ExecutionOutcomeUnknown

    runtime = ColabRuntime("https://runtime", "token")
    client = mocker.Mock()
    client.session.msg.return_value = {"header": {"msg_id": "original"}}
    mocker.patch.object(
        runtime, "_wait_for_execution", return_value={"content": {"status": "ok"}}
    )
    client.shell_channel.send.side_effect = OSError("socket broke during write")
    with pytest.raises(ExecutionOutcomeUnknown, match="has not been resubmitted"):
        runtime._start_execution(client, "side_effect()")
    client.shell_channel.send.assert_called_once()


def test_journal_detects_a_missing_record_instead_of_reporting_success(mocker):
    journal = ExecutionJournal("https://runtime", "token", "original")
    response = mocker.Mock()
    response.json.return_value = {
        "format": "text",
        "content": json.dumps(
            {
                "header": {"msg_id": "reply", "msg_type": "execute_reply"},
                "parent_header": {"msg_id": "original"},
                "content": {"status": "ok"},
                "_colab_cli_sequence": 2,
            }
        )
        + "\n",
    }
    mocker.patch("colab_cli.execution_journal.requests.get", return_value=response)
    with pytest.raises(ValueError):
        journal.read(timeout=2)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {"header": []},
        {
            "header": {"msg_id": "a", "msg_type": "stream"},
            "parent_header": {"msg_id": "original"},
            "content": "invalid",
            "_colab_cli_sequence": 1,
        },
    ],
)
def test_invalid_record_shapes_fail_as_recovery_errors(mocker, record):
    journal = ExecutionJournal("https://runtime", "token", "original")
    response = mocker.Mock()
    response.json.return_value = {
        "format": "text",
        "content": json.dumps(record) + "\n",
    }
    mocker.patch("colab_cli.execution_journal.requests.get", return_value=response)
    with pytest.raises(ValueError):
        journal.read(timeout=1)
