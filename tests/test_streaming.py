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

import pytest
from mock_kernel import (
    MockKernelClient,
    connect_runtime,
    reply_message,
    status_message,
    stdout_message,
)

from colab_cli.runtime import ColabRuntime


@pytest.fixture
def run(monkeypatch):
    """Run execute_code against a MockKernelClient; returns (outputs, streamed)."""

    def _run(mock_kc, code="work()", timeout=30.0):
        runtime = ColabRuntime("http://url", "token")
        connect_runtime(runtime, mock_kc, monkeypatch)
        streamed = []
        outputs = runtime.execute_code(
            code, output_hook=streamed.append, timeout=timeout
        )
        return outputs, streamed

    return _run


def test_runtime_execute_code_streaming(run):
    mock_kc = MockKernelClient(
        [
            (0.1, stdout_message("1\n")),
            (0.2, stdout_message("2\n")),
            (0.3, status_message("idle")),
        ]
    )
    outputs, streamed = run(mock_kc, code="print(1); print(2)")

    assert [o["text"] for o in outputs] == ["1\n", "2\n"]
    assert [o["text"] for o in streamed] == ["1\n", "2\n"]
    assert mock_kc.code == "print(1); print(2)"
    assert mock_kc.execute_kwargs == {"allow_stdin": False}


def test_runtime_execute_code_streaming_error_synthesis(run):
    reply = reply_message(
        "error", ename="RuntimeError", evalue="something went wrong", traceback=["tb"]
    )
    mock_kc = MockKernelClient([(0.1, status_message("idle"))], shell=[(0.1, reply)])
    outputs, streamed = run(mock_kc, code="fail()")

    # The synthesized error is added after the run, so it is returned but not streamed.
    assert len(outputs) == 1
    assert outputs[0]["output_type"] == "error"
    assert outputs[0]["ename"] == "RuntimeError"
    assert streamed == []


def test_a_run_that_keeps_printing_is_not_cut_off(run):
    """A 10-minute run that prints every 20 s finishes under a 30 s idle limit."""
    lines = [(t, stdout_message(f"step {t}\n")) for t in range(20, 601, 20)]
    mock_kc = MockKernelClient([*lines, (601, status_message("idle"))])
    outputs, _ = run(mock_kc, timeout=30.0)

    assert "".join(o["text"] for o in outputs).count("step") == 30


def test_silence_longer_than_the_timeout_raises(run):
    mock_kc = MockKernelClient(
        [(5, stdout_message("loading\n")), (100, status_message("idle"))]
    )
    with pytest.raises(TimeoutError, match="Timeout waiting for output.*--timeout"):
        run(mock_kc, timeout=30.0)
    # 30 s after the last message, not 30 s after the start.
    assert 35 <= mock_kc.now <= 36


@pytest.mark.parametrize("timeout", [None, 0])
def test_no_limit_waits_through_any_silence(run, timeout):
    mock_kc = MockKernelClient(
        [(3600, stdout_message("done\n")), (3600.5, status_message("idle"))]
    )
    outputs, _ = run(mock_kc, timeout=timeout)
    assert outputs[0]["text"] == "done\n"


def test_the_reply_may_arrive_after_the_idle_status(run):
    mock_kc = MockKernelClient(
        [(5, stdout_message("x\n")), (6, status_message("idle"))],
        shell=[(26, reply_message())],
    )
    outputs, _ = run(mock_kc, timeout=30.0)
    assert outputs[0]["text"] == "x\n"


def test_a_reply_that_never_arrives_times_out(run):
    mock_kc = MockKernelClient([(5, status_message("idle"))], shell=[])
    with pytest.raises(TimeoutError, match="Timeout waiting for reply"):
        run(mock_kc, timeout=30.0)


def test_messages_from_other_requests_are_ignored(run):
    mock_kc = MockKernelClient(
        [
            (1, status_message("idle", parent="req-0")),
            (2, stdout_message("ours\n")),
            (3, status_message("idle")),
        ],
        shell=[(1, reply_message("error", parent="req-0")), (3, reply_message())],
    )
    outputs, _ = run(mock_kc, timeout=30.0)
    assert [o["text"] for o in outputs] == ["ours\n"]


def test_output_from_other_requests_does_not_reset_the_timeout(run):
    others = [(t, stdout_message("other\n", parent="req-0")) for t in range(5, 100, 5)]
    mock_kc = MockKernelClient([*others, (100, status_message("idle"))])
    with pytest.raises(TimeoutError):
        run(mock_kc, timeout=30.0)
    assert 30 <= mock_kc.now <= 31


def test_waiting_never_spins(run):
    """Each wait blocks for up to a second, so a long run costs a few calls a second."""
    lines = [(t, stdout_message("tick\n")) for t in range(25, 301, 25)]
    mock_kc = MockKernelClient([*lines, (301, status_message("idle"))])
    run(mock_kc, timeout=30.0)
    assert 0 < len(mock_kc.waits) < 330  # about one wait per second over 300 s


def test_a_lost_connection_stops_the_wait(run):
    mock_kc = MockKernelClient(
        [(5, stdout_message("x\n")), (100, status_message("idle"))], disconnect_at=10
    )
    with pytest.raises(RuntimeError, match="Connection was lost"):
        run(mock_kc, timeout=None)
    assert mock_kc.now < 12


@pytest.mark.parametrize("state", ["dead", "restarting"])
def test_a_kernel_that_dies_or_restarts_ends_the_wait(run, state):
    mock_kc = MockKernelClient(
        [
            (5, stdout_message("x\n")),
            (8, status_message(state, parent=None)),
            (100, status_message("idle")),
        ]
    )
    with pytest.raises(RuntimeError, match=f"Kernel {state}"):
        run(mock_kc, timeout=None)
