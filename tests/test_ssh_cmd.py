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

"""`colab ssh -- CMD`: run one command on an existing runtime over ssh.

CMD is forwarded verbatim, stdout stays clean, neither the command nor its
ProxyCommand child creates a runtime, and a PTY is requested only when stdin
and stdout are terminals.
"""

import os
import shlex
import sys
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

from colab_cli.cli import app
from colab_cli.commands import execution
from colab_cli.commands import ssh as ssh_module
from colab_cli.common import State
import pytest
from typer.testing import CliRunner

runner = CliRunner()
_PREFIX = f"cd {ssh_module._DEFAULT_REMOTE_DIR} 2>/dev/null; "


def _make_session():
    s = MagicMock(url="https://abc.colab.googleusercontent.com", token="TOK")
    s.name = "s1"
    return s


@pytest.fixture(autouse=True)
def one_session(mock_common_state, mocker):
    session = _make_session()
    mock_common_state.store.get.return_value = session
    mock_common_state.store.list.return_value = {"s1": session}
    mock_common_state.sync_sessions.return_value = ({"s1": session}, [])

    def resolve(name):
        return State.resolve_session(mock_common_state, name)

    mock_common_state.resolve_session.side_effect = resolve
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    return mock_common_state


@pytest.mark.parametrize(
    ("argv", "line", "rc"),
    [
        (["--", "python", "-c", "print(1)"], "python -c 'print(1)'", 0),
        (["--", "echo", "-i", "--rm"], "echo -i --rm", 1),
        (["grep", "-i", "foo"], "grep -i foo", 7),
        (["--", "ls / | wc -l"], "ls / | wc -l", 255),
        (["--", "ls", "*.py", "$HOME"], "ls '*.py' '$HOME'", 0),
    ],
    ids=["python-c", "colab-flags", "no-dashes", "one-arg", "glob-var"],
)
def test_ssh_cmd_forwards_argv_and_exit_code(mocker, argv, line, rc):
    call = mocker.patch("subprocess.call", return_value=rc)
    result = runner.invoke(app, ["ssh", "-s", "s1", *argv])
    assert result.exit_code == rc, result.output
    args = call.call_args.args[0]
    assert args[-1] == _PREFIX + line
    cmd = argv[1:] if argv[0] == "--" else argv
    if len(cmd) > 1:
        assert shlex.split(line) == cmd
    assert "-i" not in args[:-1]


@pytest.mark.parametrize(
    ("stdin", "stdout", "flag"),
    [
        (True, True, "-t"),
        (True, False, "-T"),
        (False, True, "-T"),
        (False, False, "-T"),
    ],
    ids=["both-tty", "stdout-piped", "stdin-piped", "both-piped"],
)
def test_run_remote_command_pty_only_when_both_tty(mocker, stdin, stdout, flag):
    mocker.patch.object(execution, "is_stdin_tty", return_value=stdin)
    mocker.patch.object(execution, "is_stdout_tty", return_value=stdout)
    call = mocker.patch("subprocess.call", return_value=0)
    ssh_module._run_remote_command(_make_session(), None, ["true"])
    args = call.call_args.args[0]
    assert [a for a in args if a in ("-t", "-T")] == [flag]
    assert args[args.index(ssh_module._SSH_HOST) - 1] == flag


def test_ssh_cmd_proxy_mode_exits_2_without_connecting(one_session, mocker):
    connect = mocker.patch("websocket.WebSocket.connect")
    call = mocker.patch("subprocess.call")
    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1", "--", "ls"])
    assert result.exit_code == 2
    assert "--proxy-mode" in result.stderr
    connect.assert_not_called()
    call.assert_not_called()
    one_session.store.get.assert_not_called()


@pytest.mark.parametrize(
    ("listed", "code", "message"),
    [(False, 1, "No active sessions"), (True, 0, "Using unique session 's1'")],
    ids=["empty-store", "unique-session"],
)
def test_ssh_cmd_session_lookup_stays_off_stdout(
    one_session, mocker, listed, code, message
):
    if not listed:
        one_session.store.list.return_value = {}
    new = mocker.patch("colab_cli.commands.session.new")
    mocker.patch("subprocess.call", return_value=0)
    result = runner.invoke(app, ["ssh", "--", "echo", "hi"])
    assert result.exit_code == code
    assert result.stdout == ""
    assert message in result.stderr
    new.assert_not_called()


def test_ssh_cmd_proxy_child_tags_handshake_with_cmd_mode(mocker):
    call = mocker.patch("subprocess.call", return_value=0)
    ssh_module._run_remote_command(_make_session(), None, ["true"])
    args = call.call_args.args[0]
    child = shlex.split(args[args.index("-o") + 1].split("=", 1)[1])
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value="pk")
    connect = mocker.patch(
        "websocket.WebSocket.connect", side_effect=ConnectionRefusedError
    )
    result = runner.invoke(app, child[child.index("ssh") :])
    assert result.exit_code == 1
    query = parse_qs(urlparse(connect.call_args.args[0]).query)
    assert set(query) == {"colab-runtime-proxy-token", "colab-client-agent"}
    assert query["colab-client-agent"] == ["colab-cli", "ssh-mode-cmd"]
    header = connect.call_args.kwargs["header"]
    assert "X-Colab-Client-Agent: colab-cli" in header


def test_ssh_cmd_proxy_child_never_creates_session(one_session, mocker):
    one_session.store.get.return_value = None
    new = mocker.patch("colab_cli.commands.session.new")
    connect = mocker.patch("websocket.WebSocket.connect")
    proxy_command = ssh_module._proxy_command(
        _make_session(), None, ssh_module._CMD_SSH_MODE
    )
    child = shlex.split(proxy_command)
    result = runner.invoke(app, child[child.index("ssh") :])
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "not found" in result.stderr
    new.assert_not_called()
    connect.assert_not_called()


def test_ssh_cmd_with_closed_stdio_runs_without_pty(mocker):
    mocker.patch.object(execution, "sys", MagicMock(stdin=None, stdout=None))
    check = mocker.patch("colab_cli.auto_update.run_background_check")
    call = mocker.patch("subprocess.call", return_value=0)
    result = runner.invoke(app, ["ssh", "-s", "s1", "--", "cat"])
    assert result.exit_code == 0, result.output
    assert "-T" in call.call_args.args[0]
    check.assert_not_called()


@pytest.mark.parametrize("extra", [[], ["--"]], ids=["no-cmd", "bare-dashes"])
def test_ssh_without_cmd_builds_interactive_argv(mocker, extra):
    call = mocker.patch("subprocess.call", return_value=0)
    assert runner.invoke(app, ["ssh", "-s", "s1", *extra]).exit_code == 0
    proxy = [sys.executable, "-m", "colab_cli.cli", "ssh", "--proxy-mode"]
    proxy += ["-s", "s1", "--colab-internal-mode", "interactive"]
    opts = f"StrictHostKeyChecking=no -o UserKnownHostsFile={os.devnull}"
    assert call.call_args.args[0] == [
        "ssh",
        "-o",
        f"ProxyCommand={shlex.join(proxy)}",
        *f"-o {opts} -o LogLevel=ERROR -t root@colab-runtime".split(),
        "cd /content 2>/dev/null; exec ${SHELL:-/bin/bash} -l",
    ]


@pytest.mark.parametrize(
    ("cmd", "tty"),
    [([], True), ([], False), (["--", "true"], True), (["--", "true"], False)],
    ids=["shell-tty", "shell-piped", "cmd-tty", "cmd-piped"],
)
def test_cli_update_check_for_ssh_follows_stdout_tty(mocker, cmd, tty):
    mocker.patch.object(execution, "is_stdout_tty", return_value=tty)
    check = mocker.patch("colab_cli.auto_update.run_background_check")
    mocker.patch("subprocess.call", return_value=0)
    runner.invoke(app, ["ssh", "-s", "s1", *cmd])
    assert check.called is tty
