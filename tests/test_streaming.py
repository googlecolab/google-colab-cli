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
from mock_kernel import MockKernelClient, _idle, _reply, _stdout, connect_runtime

from colab_cli.runtime import ColabRuntime


@pytest.fixture
def run(monkeypatch):
    """Run execute_code against a MockKernelClient; returns (outputs, streamed, mock_kc)."""

    def _run(mock_kc, code="work()", timeout=30.0):
        runtime = ColabRuntime("http://url", "token")
        connect_runtime(runtime, mock_kc, monkeypatch)
        streamed = []
        outputs = runtime.execute_code(
            code, output_hook=streamed.append, timeout=timeout
        )
        return outputs, streamed, mock_kc

    return _run


def test_runtime_execute_code_streaming(run):
    mock_kc = MockKernelClient(
        [(0.1, _stdout("1\n")), (0.2, _stdout("2\n")), (0.3, _idle())]
    )
    outputs, streamed, _ = run(mock_kc, code="print(1); print(2)")

    assert [o["text"] for o in outputs] == ["1\n", "2\n"]
    assert [o["text"] for o in streamed] == ["1\n", "2\n"]
    assert mock_kc.code == "print(1); print(2)"


def test_runtime_execute_code_streaming_error_synthesis(run):
    reply = _reply(
        "error", ename="RuntimeError", evalue="something went wrong", traceback=["tb"]
    )
    mock_kc = MockKernelClient([(0.1, _idle())], shell=[(0.1, reply)])
    outputs, streamed, _ = run(mock_kc, code="fail()")

    # The synthesized error is added after the run, so it is returned but not streamed.
    assert len(outputs) == 1
    assert outputs[0]["output_type"] == "error"
    assert outputs[0]["ename"] == "RuntimeError"
    assert streamed == []


def test_a_run_that_keeps_printing_is_not_cut_off(run):
    """--timeout is an idle limit: a 10-minute run that prints every 20 s finishes."""
    lines = [(t, _stdout(f"step {t}\n")) for t in range(20, 601, 20)]
    mock_kc = MockKernelClient([*lines, (601, _idle())])
    outputs, _, _ = run(mock_kc, timeout=30.0)

    assert "".join(o["text"] for o in outputs).count("step") == 30


def test_silence_longer_than_the_timeout_raises(run):
    mock_kc = MockKernelClient([(5, _stdout("loading\n")), (100, _idle())])
    with pytest.raises(TimeoutError, match="Timeout waiting for output"):
        run(mock_kc, timeout=30.0)
    assert (
        35 <= mock_kc.now <= 36
    )  # 30 s after the last message, not 30 s after the start


def test_no_timeout_waits_through_any_silence(run):
    mock_kc = MockKernelClient([(3600, _stdout("done\n")), (3600.5, _idle())])
    outputs, _, _ = run(mock_kc, timeout=None)
    assert outputs[0]["text"] == "done\n"


def test_waiting_never_spins(run):
    """Each wait blocks for up to a second, so a long run costs a few calls a second."""
    lines = [(t, _stdout("tick\n")) for t in range(25, 301, 25)]
    mock_kc = MockKernelClient([*lines, (301, _idle())])
    run(mock_kc, timeout=30.0)
    assert mock_kc.waits and all(w > 0 for w in mock_kc.waits)
    assert len(mock_kc.waits) < 330  # about one wait per second over 300 s


def test_messages_from_other_requests_do_not_count_as_output(run):
    others = [(t, _stdout("other\n", parent="req-0")) for t in range(5, 100, 5)]
    mock_kc = MockKernelClient([*others, (100, _idle())])
    with pytest.raises(TimeoutError):
        run(mock_kc, timeout=30.0)
    assert 30 <= mock_kc.now <= 31


def test_a_lost_connection_stops_the_wait(run):
    mock_kc = MockKernelClient([(5, _stdout("x\n")), (100, _idle())])
    mock_kc.connection_ready.clear()
    with pytest.raises(RuntimeError, match="Connection was lost"):
        run(mock_kc, timeout=None)
