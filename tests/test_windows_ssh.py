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

"""Native Windows regressions for the OpenSSH transport."""

import json
import os
import signal
import shutil
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest

from colab_cli.commands import ssh


@pytest.fixture(scope="module")
def windows_python(tmp_path_factory):
    path = tmp_path_factory.mktemp("python") / "Python with spaces"
    venv.EnvBuilder().create(path)
    return str(path / "Scripts" / "python.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="requires CreateProcessW")
@pytest.mark.parametrize(
    "name",
    [
        "simple",
        "with space",
        "a'b",
        'a"b',
        "$(echo test)",
        "a;b",
        "a&b",
        "a\\b\\",
        "\u6d4b\u8bd5",
    ],
)
@pytest.mark.parametrize("identity", [None, "C:\\SSH keys\\id_ed25519"])
def test_proxy_command_launches_with_literal_arguments(
    tmp_path, windows_python, monkeypatch, name, identity
):
    # A real child process parses the command using Windows argument rules.
    package = tmp_path / "colab_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import json, sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8"
    )
    monkeypatch.setattr(ssh.sys, "executable", windows_python)
    command = ssh._proxy_command(SimpleNamespace(name=name), identity)
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    expected = ["ssh", "--proxy-mode", "-s", name]
    if identity:
        expected.extend(["--identity", identity])
    assert json.loads(result.stdout) == expected


@pytest.mark.skipif(
    sys.platform != "win32" or not shutil.which("ssh"),
    reason="requires Windows OpenSSH",
)
def test_windows_openssh_starts_proxy(tmp_path, windows_python, monkeypatch):
    package = tmp_path / "colab_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['COLAB_TEST_ARGV']).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    argv_file = tmp_path / "argv.json"
    monkeypatch.setattr(ssh.sys, "executable", windows_python)
    command = ssh._proxy_command(
        SimpleNamespace(name="session with spaces"), "C:\\SSH keys\\id_ed25519"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "COLAB_TEST_ARGV": str(argv_file)}
    # The stub exits before the handshake; only the actual ProxyCommand launch
    # and its arguments are under test. No network connection is made.
    subprocess.run(
        [
            "ssh",
            "-F",
            os.devnull,
            "-o",
            f"ProxyCommand={command}",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "unused-host",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert json.loads(argv_file.read_text(encoding="utf-8")) == [
        "ssh",
        "--proxy-mode",
        "-s",
        "session with spaces",
        "--identity",
        "C:\\SSH keys\\id_ed25519",
    ]


def test_rm_handlers_without_sighup(mocker):
    supported = SimpleNamespace(SIGTERM=15, SIGINT=2, signal=mocker.Mock())
    mocker.patch.object(ssh, "signal", supported)
    cleanup = mocker.Mock()
    exit_process = mocker.patch.object(ssh.os, "_exit")

    ssh._install_rm_signal_handlers(cleanup)

    handlers = {call.args[0]: call.args[1] for call in supported.signal.call_args_list}
    assert set(handlers) == {supported.SIGTERM, supported.SIGINT}
    handlers[supported.SIGTERM](supported.SIGTERM, None)
    cleanup.assert_called_once_with()
    exit_process.assert_called_once_with(0)


def test_ssh_uses_local_null_device():
    args = ssh._ssh_base_args("proxy", None)
    assert f"UserKnownHostsFile={os.devnull}" in args


def test_rm_handlers_register_available_signals(mocker):
    register = mocker.patch.object(ssh.signal, "signal")
    ssh._install_rm_signal_handlers(mocker.Mock())
    expected = {
        getattr(signal, name)
        for name in ("SIGHUP", "SIGTERM", "SIGINT")
        if hasattr(signal, name)
    }
    assert {call.args[0] for call in register.call_args_list} == expected
