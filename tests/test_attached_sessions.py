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

from datetime import datetime, timedelta, timezone

import pytest
import typer.main
from session_fakes import ATT, fresh_session, use_guarded_client
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import automation as automation_cmd
from colab_cli.commands import execution as execution_cmd
from colab_cli.commands import run as run_cmd
from colab_cli.commands import session as session_cmd
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
