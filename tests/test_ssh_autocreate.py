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

"""Tests for `colab ssh` auto-create-when-no-session behavior.

Bare `colab ssh` (no -s) should:
  * ssh into the single existing session if there is exactly one (unchanged),
  * auto-run `colab new` then ssh in if there are zero sessions,
  * still error as ambiguous if there are multiple.
Plus `--gpu/--tpu` passthrough to the auto-created runtime and `--rm` to stop a
runtime that `colab ssh` created when the shell exits. `--proxy-mode` never
auto-creates (it is a plumbing mode whose stdout is the ssh byte stream).
"""

from unittest.mock import MagicMock

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module
import typer
from typer.testing import CliRunner

runner = CliRunner()


def _make_session(
    name: str = "auto1",
    url: str = "https://abc.colab.googleusercontent.com",
    token: str = "TOK",
    endpoint: str = "ep1",
):
    s = MagicMock()
    s.name = name
    s.url = url
    s.token = token
    s.endpoint = endpoint
    return s


def test_bare_ssh_no_session_auto_creates_then_connects(
    mock_common_state, mocker
):
    """Bare `colab ssh` with zero sessions runs `colab new` then sshes in."""
    mock_common_state.store.list.return_value = {}  # no sessions -> create
    sess = _make_session()
    mock_common_state.store.get.return_value = sess

    new = mocker.patch("colab_cli.commands.session.new")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    interactive = mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )

    result = runner.invoke(app, ["ssh"])
    assert result.exit_code == 0
    new.assert_called_once()
    # CPU default: no accelerator requested.
    assert new.call_args.kwargs.get("gpu") is None
    assert new.call_args.kwargs.get("tpu") is None
    interactive.assert_called_once_with(sess, None)


def test_bare_ssh_single_session_reuses_without_create(
    mock_common_state, mocker
):
    """One existing session -> ssh into it, do NOT create a new one."""
    mock_common_state.store.list.return_value = {"only": _make_session("only")}
    sess = _make_session("only")
    mock_common_state.resolve_session.return_value = "only"
    mock_common_state.store.get.return_value = sess

    new = mocker.patch("colab_cli.commands.session.new")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    interactive = mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )

    result = runner.invoke(app, ["ssh"])
    assert result.exit_code == 0
    new.assert_not_called()
    interactive.assert_called_once_with(sess, None)


def test_bare_ssh_multiple_sessions_does_not_create(mock_common_state, mocker):
    """Multiple sessions -> ambiguous error, no auto-create."""
    mock_common_state.store.list.return_value = {
        "a": _make_session("a"),
        "b": _make_session("b"),
    }
    mock_common_state.resolve_session.side_effect = typer.Exit(1)

    new = mocker.patch("colab_cli.commands.session.new")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    interactive = mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )

    result = runner.invoke(app, ["ssh"])
    assert result.exit_code != 0
    new.assert_not_called()
    interactive.assert_not_called()


def test_bare_ssh_gpu_passthrough_on_autocreate(mock_common_state, mocker):
    """`colab ssh --gpu T4` creates the runtime with that accelerator."""
    mock_common_state.store.list.return_value = {}
    mock_common_state.store.get.return_value = _make_session()
    new = mocker.patch("colab_cli.commands.session.new")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(ssh_module, "_run_interactive_ssh", return_value=0)

    result = runner.invoke(app, ["ssh", "--gpu", "T4"])
    assert result.exit_code == 0
    new.assert_called_once()
    assert new.call_args.kwargs.get("gpu") == "T4"


def test_rm_stops_autocreated_session_on_exit(mock_common_state, mocker):
    """`--rm` stops the runtime `colab ssh` created once the shell exits."""
    mock_common_state.store.list.return_value = {}
    mock_common_state.store.get.return_value = _make_session("auto-rm")
    mocker.patch("colab_cli.commands.session.new")
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(ssh_module, "_run_interactive_ssh", return_value=0)

    result = runner.invoke(app, ["ssh", "--rm"])
    assert result.exit_code == 0
    stop.assert_called_once_with(session="auto-rm")


def test_rm_does_not_stop_reused_session(mock_common_state, mocker):
    """`--rm` must NOT stop a pre-existing session the user already had."""
    mock_common_state.store.list.return_value = {"only": _make_session("only")}
    mock_common_state.resolve_session.return_value = "only"
    mock_common_state.store.get.return_value = _make_session("only")
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(ssh_module, "_run_interactive_ssh", return_value=0)

    result = runner.invoke(app, ["ssh", "--rm"])
    assert result.exit_code == 0
    stop.assert_not_called()


def test_proxy_mode_no_session_does_not_autocreate(mock_common_state, mocker):
    """--proxy-mode with no -s does not auto-create (nothing to name)."""
    mock_common_state.store.list.return_value = {}
    mock_common_state.resolve_session.side_effect = typer.Exit(2)
    new = mocker.patch("colab_cli.commands.session.new")
    connect = mocker.patch.object(ssh_module, "_connect_websocket")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )

    result = runner.invoke(app, ["ssh", "--proxy-mode"])
    assert result.exit_code != 0
    new.assert_not_called()
    connect.assert_not_called()


def test_ssh_help_advertises_autocreate_flags():
    """`colab ssh --help` advertises --rm, --gpu, and --tpu."""
    result = runner.invoke(app, ["ssh", "--help"])
    assert result.exit_code == 0
    assert "--rm" in result.output
    assert "--gpu" in result.output
    assert "--tpu" in result.output


# --- --proxy-mode honors every flag (the ~/.ssh/config use case) -------------


def test_proxy_mode_creates_missing_named_session(mock_common_state, mocker):
    """`--proxy-mode -s NAME` creates NAME if missing so a config host works on
    first connect; it then bridges and (no --rm) leaves the runtime running."""
    created = _make_session("colab")
    mock_common_state.store.get.return_value = None  # NAME missing -> create

    def after_new(*a, **k):
        mock_common_state.store.get.return_value = created

    new = mocker.patch("colab_cli.commands.session.new", side_effect=after_new)
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "colab"])
    assert result.exit_code == 0
    new.assert_called_once()
    assert new.call_args.kwargs.get("session") == "colab"
    stop.assert_not_called()  # no --rm -> runtime persists


def test_proxy_mode_gpu_passthrough_on_create(mock_common_state, mocker):
    """`--proxy-mode -s NAME --gpu T4` creates NAME with that accelerator."""
    created = _make_session("colab-gpu")
    mock_common_state.store.get.return_value = None

    def after_new(*a, **k):
        mock_common_state.store.get.return_value = created

    new = mocker.patch("colab_cli.commands.session.new", side_effect=after_new)
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(
        app, ["ssh", "--proxy-mode", "-s", "colab-gpu", "--gpu", "T4"]
    )
    assert result.exit_code == 0
    new.assert_called_once()
    assert new.call_args.kwargs.get("session") == "colab-gpu"
    assert new.call_args.kwargs.get("gpu") == "T4"


def test_proxy_mode_existing_named_session_not_recreated(
    mock_common_state, mocker
):
    """`--proxy-mode -s NAME` reuses an existing NAME (no duplicate runtime)."""
    mock_common_state.store.get.return_value = _make_session("colab")
    mock_common_state.resolve_session.return_value = "colab"
    new = mocker.patch("colab_cli.commands.session.new")
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "colab"])
    assert result.exit_code == 0
    new.assert_not_called()
    stop.assert_not_called()


def test_proxy_mode_rm_stops_bridged_session_on_disconnect(
    mock_common_state, mocker
):
    """`--proxy-mode -s NAME --rm` stops the session when the bridge closes."""
    created = _make_session("colab-ephem")
    mock_common_state.store.get.return_value = None

    def after_new(*a, **k):
        mock_common_state.store.get.return_value = created

    mocker.patch("colab_cli.commands.session.new", side_effect=after_new)
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch("signal.signal")  # don't install real handlers during tests
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(
        app, ["ssh", "--proxy-mode", "-s", "colab-ephem", "--rm"]
    )
    assert result.exit_code == 0
    stop.assert_called_once_with(session="colab-ephem")


def test_proxy_mode_rm_installs_sighup_cleanup_handler(
    mock_common_state, mocker
):
    """--rm installs a SIGHUP handler so teardown runs when OpenSSH HUPs the
    ProxyCommand on disconnect (Python's default SIGHUP skips the finally)."""
    import signal as _signal

    mock_common_state.store.get.return_value = _make_session("colab-ephem")
    mock_common_state.resolve_session.return_value = "colab-ephem"
    sigmock = mocker.patch("signal.signal")
    mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(
        app, ["ssh", "--proxy-mode", "-s", "colab-ephem", "--rm"]
    )
    assert result.exit_code == 0
    registered = [c.args[0] for c in sigmock.call_args_list]
    assert _signal.SIGHUP in registered


def test_proxy_mode_without_rm_installs_no_signal_handler(
    mock_common_state, mocker
):
    """No --rm -> no signal handlers installed (keeps default behavior)."""
    mock_common_state.store.get.return_value = _make_session("colab")
    mock_common_state.resolve_session.return_value = "colab"
    sigmock = mocker.patch("signal.signal")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "colab"])
    assert result.exit_code == 0
    sigmock.assert_not_called()


def test_proxy_mode_without_rm_does_not_stop(mock_common_state, mocker):
    """`--proxy-mode` without --rm never stops the bridged session."""
    mock_common_state.store.get.return_value = _make_session("colab")
    mock_common_state.resolve_session.return_value = "colab"
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "colab"])
    assert result.exit_code == 0
    stop.assert_not_called()
