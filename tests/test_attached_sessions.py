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

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import typer
import typer.main
from session_fakes import (
    ATT,
    UnassignSession,
    fresh_session,
    unassigned_endpoints,
    use_guarded_client,
)
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import automation as automation_cmd
from colab_cli.commands import execution as execution_cmd
from colab_cli.commands import run as run_cmd
from colab_cli.commands import session as session_cmd
from colab_cli.commands import ssh as ssh_module
from colab_cli.state import StateStore

runner = CliRunner()

ATTACHED_ALLOWED = {"ssh", "stop", "sessions", "status"}
SESSION_INDEPENDENT = set(
    "help readme README skill SKILL version update whoami pay usage log".split()
)
ARGLESS = "auth console drivemount exec ls new repl restart-kernel url".split()
ATTACHED_REFUSED = dict.fromkeys(ARGLESS, ()) | {
    "download": ("x", "y"),
    "edit": ("x",),
    "install": ("pkg",),
    "rm": ("x",),
    "run": ("job.py",),
    "upload": ("job.py", "x"),
}


def test_every_command_is_classified_for_attached_sessions():
    registered = set(typer.main.get_group(app).commands)

    listed = ATTACHED_ALLOWED | SESSION_INDEPENDENT | set(ATTACHED_REFUSED)
    assert registered == listed


def _store_bytes(tmp_path):
    files = (tmp_path / "sessions.json", tmp_path / "sessions.attached.json")
    return [f.read_bytes() if f.exists() else None for f in files]


def _assert_no_kernel_or_contents_request(st, contents):
    assert st.client.session.requests == []
    contents.assert_not_called()
    for module in (session_cmd, execution_cmd, automation_cmd, run_cmd):
        module.ColabRuntime.assert_not_called()


@pytest.mark.parametrize("command", sorted(ATTACHED_REFUSED))
def test_attached_session_is_ssh_only(
    two_file_state, tmp_path, monkeypatch, mocker, command
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.py").write_text("print(1)\n")
    contents = mocker.patch("colab_cli.contents.requests.request")
    before = _store_bytes(tmp_path)

    args = [command, "-s", "att", *ATTACHED_REFUSED[command]]
    result = runner.invoke(app, args, input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    _assert_no_kernel_or_contents_request(two_file_state, contents)
    assert _store_bytes(tmp_path) == before


def test_refusal_precedes_token_refresh(real_state, tmp_path):
    real_state._store = StateStore(str(tmp_path / "sessions.json"))
    expired = fresh_session(*ATT)
    expired.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    real_state.store.add(fresh_session("cli", "cli-ep", False))
    real_state.store.add(expired)
    before = _store_bytes(tmp_path)

    result = runner.invoke(app, ["exec", "-s", "att"], input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    assert real_state.client.session.requests == []
    assert _store_bytes(tmp_path) == before


def test_teardown_reads_attached_record_without_refusal(
    two_file_state, tmp_path, mocker, capsys
):
    use_guarded_client(two_file_state, mocker)
    before = _store_bytes(tmp_path)

    run_cmd._teardown("att", fresh_session(*ATT), reason="script_exit")

    assert "colab ssh -s att" not in capsys.readouterr().err
    assert two_file_state.client.session.requests == []
    run_cmd.ColabRuntime.assert_not_called()
    assert _store_bytes(tmp_path) == before


class _AssignmentsSession(UnassignSession):
    """Records requests, lists `endpoints` as assigned and accepts unassign."""

    def __init__(self, *endpoints):
        super().__init__()
        self.endpoints = endpoints

    def request(self, method, url, **kwargs):
        if not url.endswith("/tun/m/assignments"):
            return super().request(method, url, **kwargs)
        self.requests.append((method, url))
        info = {"token": "t", "tokenExpiresInSeconds": 3600}
        listed = [
            {
                "accelerator": "NONE",
                "endpoint": e,
                "variant": 0,
                "machineShape": 0,
                "runtimeProxyInfo": dict(info, url=f"https://{e}"),
            }
            for e in self.endpoints
        ]
        text = json.dumps({"assignments": listed})
        return MagicMock(ok=True, status_code=200, reason="OK", text=text)


def _store(st, tmp_path, mocker, records, assigned):
    st._store = StateStore(str(tmp_path / "sessions.json"))
    for args in records:
        st.store.add(fresh_session(*args))
    use_guarded_client(st, mocker, _AssignmentsSession(*assigned))


CLI = ("cli", "cli-ep", False)
ATT2 = ("att2", "att2-ep", True)
MIXED = [ATT, CLI]
BOTH_ASSIGNED = ["att-ep", "cli-ep"]


@pytest.mark.parametrize("command", ["restart-kernel", "url"])
def test_unique_pick_skips_attached(real_state, tmp_path, mocker, command):
    _store(real_state, tmp_path, mocker, MIXED, BOTH_ASSIGNED)

    result = runner.invoke(app, [command])

    assert result.exit_code == 0, result.output
    assert "Using unique session 'cli'" in result.output
    assert "att-ep" not in result.output
    assert not any("att-ep" in u for _, u in real_state.client.session.requests)
    if command == "restart-kernel":
        assert session_cmd.ColabRuntime.call_args.args[0] == "https://cli-ep"


def test_bare_stop_releases_only_the_cli_runtime(real_state, tmp_path, mocker):
    _store(real_state, tmp_path, mocker, MIXED, BOTH_ASSIGNED)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert unassigned_endpoints(real_state) == {"cli-ep"}
    assert real_state.store.get("att").attached


PICK_COMMANDS = ["stop", *sorted(set(ATTACHED_REFUSED) - {"new", "run"})]


@pytest.mark.parametrize("records", [[ATT], [ATT, ATT2]])
@pytest.mark.parametrize("command", PICK_COMMANDS)
def test_only_attached_sessions_need_an_explicit_command(
    real_state, tmp_path, monkeypatch, mocker, command, records
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.py").write_text("print(1)\n")
    _store(real_state, tmp_path, mocker, records, [])
    contents = mocker.patch("colab_cli.contents.requests.request")
    before = _store_bytes(tmp_path)

    args = [command, *ATTACHED_REFUSED.get(command, ())]
    result = runner.invoke(app, args, input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att'" in result.output
    assert "colab stop -s att'" in result.output
    _assert_no_kernel_or_contents_request(real_state, contents)
    assert _store_bytes(tmp_path) == before


def test_stale_cli_and_attached_session_need_explicit_command(
    real_state, tmp_path, mocker
):
    _store(real_state, tmp_path, mocker, MIXED, ["att-ep"])

    result = runner.invoke(app, ["restart-kernel"])

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att'" in result.output
    assert "colab stop -s att'" in result.output
    session_cmd.ColabRuntime.assert_not_called()


def test_bare_ssh_with_cli_and_attached_is_ambiguous(
    real_state, tmp_path, mocker, capsys
):
    _store(real_state, tmp_path, mocker, MIXED, BOTH_ASSIGNED)

    with pytest.raises(typer.Exit):
        ssh_module._resolve_session(None)

    assert "Multiple active sessions found" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["att", None], ids=["named", "unique"])
def test_ssh_resolves_attached_session(real_state, tmp_path, mocker, name):
    _store(real_state, tmp_path, mocker, [ATT], ["att-ep"])

    assert ssh_module._resolve_session(name).endpoint == "att-ep"


@pytest.mark.parametrize("command", ["status", "sessions"])
def test_bare_listing_shows_attached(real_state, tmp_path, mocker, command):
    _store(real_state, tmp_path, mocker, [ATT], ["att-ep"])

    result = runner.invoke(app, [command])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("[att] att-ep |")
