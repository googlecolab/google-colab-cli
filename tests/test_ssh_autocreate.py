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

"""Tests for `colab ssh` auto-create + --proxy-mode flag behavior.

Bare `colab ssh` (no -s): ssh into the single existing session, auto-create one
when there are none, or error when there are several. `--gpu/--tpu` pass through
to an auto-created runtime (and are ignored otherwise); `--rm` stops a runtime
this command created. In --proxy-mode every flag still applies: `-s NAME`
creates the session if missing, and --rm stops the bridged session on
disconnect (installing SIGHUP/SIGTERM/SIGINT handlers so teardown survives the
way OpenSSH ends a ProxyCommand).
"""

from unittest.mock import MagicMock

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module
import pytest
from typer.testing import CliRunner
import typer

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


def _patch_interactive(mocker):
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    return mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )


def _patch_proxy(mocker):
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )
    mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=MagicMock()
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)


# --- bare `colab ssh`: create / reuse / ambiguous ---------------------------


@pytest.mark.parametrize(
    ("sessions", "resolve_raises", "gpu", "expect_create", "expect_ok"),
    [
        ({}, False, None, True, True),
        ({}, False, "T4", True, True),
        ({"only": 1}, False, None, False, True),
        ({"a": 1, "b": 1}, True, None, False, False),
    ],
    ids=["zero-creates", "zero-creates-gpu", "one-reuses", "many-errors"],
)
def test_bare_ssh_session_resolution(
    mock_common_state,
    mocker,
    sessions,
    resolve_raises,
    gpu,
    expect_create,
    expect_ok,
):
    mock_common_state.store.list.return_value = sessions
    sess = _make_session()
    mock_common_state.store.get.return_value = sess
    if resolve_raises:
        mock_common_state.resolve_session.side_effect = typer.Exit(1)
    else:
        mock_common_state.resolve_session.return_value = "only"

    new = mocker.patch("colab_cli.commands.session.new")
    interactive = _patch_interactive(mocker)

    args = ["ssh"] + (["--gpu", gpu] if gpu else [])
    result = runner.invoke(app, args)

    if expect_ok:
        assert result.exit_code == 0
        interactive.assert_called_once_with(sess, None)
    else:
        assert result.exit_code != 0
        interactive.assert_not_called()

    if expect_create:
        new.assert_called_once()
        assert new.call_args.kwargs.get("gpu") == gpu
        assert new.call_args.kwargs.get("tpu") is None
    else:
        new.assert_not_called()


# --- --proxy-mode -s NAME: create-if-missing / reuse ------------------------


@pytest.mark.parametrize(
    ("exists", "gpu", "expect_new"),
    [
        (False, None, True),
        (False, "T4", True),
        (True, None, False),
    ],
    ids=["missing-creates", "missing-creates-gpu", "existing-reuses"],
)
def test_proxy_mode_create_or_reuse(
    mock_common_state, mocker, exists, gpu, expect_new
):
    created = _make_session("colab")
    if exists:
        mock_common_state.store.get.return_value = created
        mock_common_state.resolve_session.return_value = "colab"
    else:
        mock_common_state.store.get.return_value = None

    def after_new(*a, **k):
        mock_common_state.store.get.return_value = created

    new = mocker.patch("colab_cli.commands.session.new", side_effect=after_new)
    _patch_proxy(mocker)

    args = ["ssh", "--proxy-mode", "-s", "colab"]
    if gpu:
        args += ["--gpu", gpu]
    result = runner.invoke(app, args)
    assert result.exit_code == 0

    if expect_new:
        new.assert_called_once()
        assert new.call_args.kwargs.get("session") == "colab"
        assert new.call_args.kwargs.get("gpu") == gpu
    else:
        new.assert_not_called()


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


# --- --gpu/--tpu ignored when not creating ----------------------------------


@pytest.mark.parametrize(
    ("extra_args", "expect_msg"),
    [
        (
            ["--proxy-mode", "-s", "colab", "--gpu", "T4"],
            "only applies to a created runtime",
        ),
        (
            ["-s", "colab", "--tpu", "v5e1"],
            "only applies to a created runtime",
        ),
    ],
    ids=["proxy-existing", "interactive-reuse"],
)
def test_gpu_tpu_ignored_when_not_creating(
    mock_common_state, mocker, extra_args, expect_msg
):
    sess = _make_session("colab")
    mock_common_state.store.get.return_value = sess
    mock_common_state.store.list.return_value = {"colab": sess}
    mock_common_state.resolve_session.return_value = "colab"
    _patch_proxy(mocker)
    mocker.patch.object(ssh_module, "_run_interactive_ssh", return_value=0)

    result = runner.invoke(app, ["ssh", *extra_args])
    assert result.exit_code == 0
    assert expect_msg in result.stderr


# --- --rm teardown ----------------------------------------------------------


@pytest.mark.parametrize(
    ("rm", "expect_stop"),
    [(True, True), (False, False)],
    ids=["rm-stops", "no-rm-keeps"],
)
def test_proxy_mode_rm_teardown(mock_common_state, mocker, rm, expect_stop):
    created = _make_session("colab-ephem")
    mock_common_state.store.get.return_value = None

    def after_new(*a, **k):
        mock_common_state.store.get.return_value = created

    mocker.patch("colab_cli.commands.session.new", side_effect=after_new)
    stop = mocker.patch("colab_cli.commands.session.stop")
    mocker.patch("signal.signal")  # don't install real handlers during tests
    _patch_proxy(mocker)

    args = ["ssh", "--proxy-mode", "-s", "colab-ephem"]
    if rm:
        args.append("--rm")
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    if expect_stop:
        stop.assert_called_once_with(session="colab-ephem")
    else:
        stop.assert_not_called()


@pytest.mark.parametrize(
    ("has_existing", "expect_stop"),
    [(False, True), (True, False)],
    ids=["autocreated-stops", "reused-keeps"],
)
def test_interactive_rm_teardown(
    mock_common_state, mocker, has_existing, expect_stop
):
    """Interactive --rm stops only a runtime `colab ssh` auto-created."""
    if has_existing:
        sess = _make_session("only")
        mock_common_state.store.list.return_value = {"only": sess}
        mock_common_state.resolve_session.return_value = "only"
    else:
        sess = _make_session("auto-rm")
        mock_common_state.store.list.return_value = {}
    mock_common_state.store.get.return_value = sess

    mocker.patch("colab_cli.commands.session.new")
    stop = mocker.patch("colab_cli.commands.session.stop")
    _patch_interactive(mocker)

    result = runner.invoke(app, ["ssh", "--rm"])
    assert result.exit_code == 0
    if expect_stop:
        stop.assert_called_once_with(session="auto-rm")
    else:
        stop.assert_not_called()


# --- signal-handler installation (proxy-mode --rm) --------------------------


@pytest.mark.parametrize(
    ("rm", "expect_installed"),
    [(True, True), (False, False)],
    ids=["rm-installs", "no-rm-none"],
)
def test_proxy_mode_signal_handler_installation(
    mock_common_state, mocker, rm, expect_installed
):
    """--rm installs SIGHUP/SIGTERM/SIGINT handlers so teardown runs when
    OpenSSH HUPs the ProxyCommand on disconnect; without --rm, none."""
    mock_common_state.store.get.return_value = _make_session("colab")
    mock_common_state.resolve_session.return_value = "colab"
    sigmock = mocker.patch("signal.signal")
    mocker.patch("colab_cli.commands.session.stop")
    _patch_proxy(mocker)

    args = ["ssh", "--proxy-mode", "-s", "colab"]
    if rm:
        args.append("--rm")
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    if expect_installed:
        registered = {c.args[0] for c in sigmock.call_args_list}
        assert set(ssh_module._rm_teardown_signals()) <= registered
    else:
        sigmock.assert_not_called()


# --- help --------------------------------------------------------------------


def test_ssh_help_advertises_autocreate_flags(mocker):
    """`colab ssh --help` advertises --rm, --gpu, and --tpu."""
    mocker.patch("colab_cli.cli.auto_update.run_background_check")
    result = runner.invoke(app, ["ssh", "--help"], terminal_width=240)
    assert result.exit_code == 0
    for flag in ("--rm", "--gpu", "--tpu"):
        assert flag in result.output
