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

"""Lifecycle & dispatch guarantees for `colab ssh`.

Complements the unit suite (test_ssh.py), the autocreate/flag suite
(test_ssh_autocreate.py), and the real-wire suite (test_ssh_wire_contract.py)
by pinning the command's *lifecycle* contracts -- the same class of guarantees
test_run.py enforces for `colab run`:

* A. --rm teardown runs even when the bridge/shell raises (try/finally).
* B. the ssh/bridge exit code propagates to the process exit code.
* C. in --proxy-mode, create + --rm chatter stays on stderr so stdout remains
  the clean ssh byte stream (a dropped redirect would corrupt every real
  connection yet pass the mocked suite -- cf. test_ssh_wire_contract.py).
* D. auto-create failure aborts before any WebSocket connect (don't burn a VM
  then fail).
* E. --gpu/--tpu are forwarded verbatim to `colab new` (which owns precedence).
* F. --rm teardown is idempotent across the signal path and the finally path.
* G. --proxy-mode --rm stops a *reused* session too, not just an auto-created
  one.
"""

from unittest.mock import MagicMock

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module
import pytest
import typer
from typer.testing import CliRunner

runner = CliRunner()


def _make_session(
    name: str = "s1",
    url: str = "https://abc.colab.googleusercontent.com",
    token: str = "TOK",
    endpoint: str = "ep",
):
    s = MagicMock()
    s.name = name
    s.url = url
    s.token = token
    s.endpoint = endpoint
    return s


def _patch_proxy(mocker):
    """Stub the proxy-mode I/O seams (pubkey, connect, bridge)."""
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)


# --- A. --rm teardown survives an exception (try/finally guarantee) ----------


def test_proxy_mode_rm_stops_even_if_bridge_raises(mock_common_state, mocker):
    """If _bridge_proxy_mode raises, the --rm stop still runs (finally)."""
    sess = _make_session("colab-ephem")
    mock_common_state.store.get.return_value = sess
    mock_common_state.resolve_session.return_value = "colab-ephem"
    mocker.patch("signal.signal")
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(
        ssh_module, "_bridge_proxy_mode", side_effect=RuntimeError("ws died")
    )

    result = runner.invoke(
        app, ["ssh", "--proxy-mode", "-s", "colab-ephem", "--rm"]
    )
    assert result.exit_code != 0
    stop.assert_called_once_with(session="colab-ephem")


def test_interactive_rm_stops_even_if_shell_raises(mock_common_state, mocker):
    """If _run_interactive_ssh raises, an auto-created runtime is still
    stopped (finally)."""
    sess = _make_session("auto-rm")
    mock_common_state.store.list.return_value = {}  # empty -> auto-create
    mock_common_state.store.get.return_value = sess
    mocker.patch("colab_cli.commands.session.new")
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(
        ssh_module, "_run_interactive_ssh", side_effect=RuntimeError("boom")
    )

    result = runner.invoke(app, ["ssh", "--rm"])
    assert result.exit_code != 0
    stop.assert_called_once_with(session="auto-rm")


# --- B. exit-code propagation ------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 255], ids=["ok", "err", "ssh-fail"])
def test_interactive_exit_code_propagates(mock_common_state, mocker, code):
    """The interactive ssh subprocess's exit code becomes the CLI exit code."""
    sess = _make_session("s1")
    mock_common_state.store.get.return_value = sess
    mock_common_state.resolve_session.return_value = "s1"
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(ssh_module, "_run_interactive_ssh", return_value=code)

    result = runner.invoke(app, ["ssh", "-s", "s1"])
    assert result.exit_code == code


@pytest.mark.parametrize("code", [0, 42], ids=["ok", "nonzero"])
def test_proxy_mode_exit_code_propagates(mock_common_state, mocker, code):
    """The proxy-mode bridge's return code becomes the CLI exit code."""
    sess = _make_session("s1")
    mock_common_state.store.get.return_value = sess
    mock_common_state.resolve_session.return_value = "s1"
    _patch_proxy(mocker)
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=code)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert result.exit_code == code


# --- C. --proxy-mode keeps stdout clean (byte-stream integrity) --------------


def test_proxy_select_routes_create_output_to_stderr(mocker, capsys):
    """Auto-create chatter must land on stderr, never stdout -- in
    --proxy-mode stdout IS the ssh byte stream."""
    sess = _make_session("newname")
    mocker.patch.object(ssh_module, "_session_exists", return_value=False)
    mocker.patch.object(ssh_module, "_resolve_session", return_value=sess)
    mocker.patch("colab_cli.commands.session.new")

    s, created = ssh_module._select_proxy_session("newname", "T4", None)
    assert created is True and s is sess

    captured = capsys.readouterr()
    assert "Creating runtime" in captured.err
    assert "Creating runtime" not in captured.out


def test_proxy_bridge_routes_rm_output_to_stderr(mocker, capsys):
    """--rm stop chatter must land on stderr, never stdout."""
    sess = _make_session("colab")
    mocker.patch("signal.signal")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    def stop_echo(session=None):
        typer.echo("STOP-MARKER")

    mocker.patch("colab_cli.commands.session.stop", side_effect=stop_echo)

    rc = ssh_module._run_proxy_bridge(sess, None, rm=True)
    assert rc == 0

    captured = capsys.readouterr()
    assert "STOP-MARKER" in captured.err
    assert "STOP-MARKER" not in captured.out


# --- D. auto-create failure aborts before any WebSocket connect --------------


def test_proxy_mode_autocreate_failure_skips_connect(mock_common_state, mocker):
    """A failed `colab new` in --proxy-mode must not proceed to connect."""
    mock_common_state.store.get.return_value = None  # session missing
    mocker.patch("colab_cli.commands.session.new", side_effect=typer.Exit(1))
    connect = mocker.patch.object(ssh_module, "_connect_websocket")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "new"])
    assert result.exit_code != 0
    connect.assert_not_called()


def test_interactive_autocreate_failure_skips_ssh(mock_common_state, mocker):
    """A failed `colab new` in interactive mode must not spawn ssh."""
    mock_common_state.store.list.return_value = {}  # empty -> auto-create
    mocker.patch("colab_cli.commands.session.new", side_effect=typer.Exit(1))
    interactive = mocker.patch.object(ssh_module, "_run_interactive_ssh")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")

    result = runner.invoke(app, ["ssh"])
    assert result.exit_code != 0
    interactive.assert_not_called()


# --- E. --gpu + --tpu are both forwarded to `colab new` ----------------------


def test_gpu_and_tpu_both_forwarded_to_new(mock_common_state, mocker):
    """`colab ssh --gpu T4 --tpu v5e1` forwards both to `colab new`, which
    resolves precedence -- SSH does not silently drop either."""
    sess = _make_session("auto")
    mock_common_state.store.list.return_value = {}  # empty -> auto-create
    mock_common_state.store.get.return_value = sess
    new = mocker.patch("colab_cli.commands.session.new")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(ssh_module, "_run_interactive_ssh", return_value=0)

    result = runner.invoke(app, ["ssh", "--gpu", "T4", "--tpu", "v5e1"])
    assert result.exit_code == 0
    new.assert_called_once()
    assert new.call_args.kwargs.get("gpu") == "T4"
    assert new.call_args.kwargs.get("tpu") == "v5e1"


# --- F. --rm teardown is idempotent (signal path + finally path) -------------


def test_proxy_mode_rm_teardown_idempotent(mock_common_state, mocker):
    """If a signal fires mid-bridge AND the finally clean-close path runs, the
    session is stopped exactly once (the `done` guard)."""
    import signal as _signal

    sess = _make_session("colab-ephem")
    mock_common_state.store.get.return_value = sess
    mock_common_state.resolve_session.return_value = "colab-ephem"

    handlers = {}
    mocker.patch(
        "signal.signal",
        side_effect=lambda sig, h: handlers.__setitem__(sig, h),
    )
    mocker.patch("os._exit")  # keep the handler from killing the test process
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )

    def bridge_then_hup(ws):
        sig = getattr(_signal, "SIGHUP", _signal.SIGTERM)
        handlers[sig](sig, None)  # OpenSSH HUPs us on platforms with SIGHUP
        return 0

    mocker.patch.object(
        ssh_module, "_bridge_proxy_mode", side_effect=bridge_then_hup
    )

    result = runner.invoke(
        app, ["ssh", "--proxy-mode", "-s", "colab-ephem", "--rm"]
    )
    assert result.exit_code == 0
    stop.assert_called_once_with(session="colab-ephem")


# --- G. --proxy-mode --rm stops a *reused* session too -----------------------


def test_proxy_mode_rm_stops_reused_session(mock_common_state, mocker):
    """proxy-mode --rm stops the bridged session on disconnect even when it
    already existed (ephemeral ~/.ssh/config host)."""
    sess = _make_session("colab")
    mock_common_state.store.get.return_value = sess  # already exists
    mock_common_state.resolve_session.return_value = "colab"
    mocker.patch("signal.signal")
    stop = mocker.patch("colab_cli.commands.session.stop")
    _patch_proxy(mocker)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "colab", "--rm"])
    assert result.exit_code == 0
    stop.assert_called_once_with(session="colab")
