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
from types import MappingProxyType

import pytest
import typer
import typer.main
from session_fakes import (
    ATT,
    ATT2,
    CLI,
    SHARED,
    AssignmentsSession,
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

runner = CliRunner()

ATTACHED_ALLOWED = frozenset({"ssh", "stop", "sessions", "status"})
SESSION_INDEPENDENT = frozenset(
    (
        "help readme README skill SKILL version update whoami test-public-api "
        "pay usage log logout"
    ).split()
)
ARGLESS = ("auth", "drivemount", "exec", "ls", "new", "repl", "restart-kernel", "url")
ATTACHED_REFUSED = MappingProxyType(
    dict.fromkeys(ARGLESS, ())
    | {
        "download": ("x", "y"),
        "edit": ("x",),
        "install": ("pkg",),
        "rm": ("x",),
        "run": ("job.py",),
        "upload": ("job.py", "x"),
    }
)


def _assert_no_kernel_or_contents_request(local_state, contents):
    assert local_state.client.session.requests == []
    contents.assert_not_called()
    session_cmd.ColabRuntime.assert_not_called()
    execution_cmd.ColabRuntime.assert_not_called()
    automation_cmd.ColabRuntime.assert_not_called()
    run_cmd.ColabRuntime.assert_not_called()


def test_every_command_is_classified_for_attached_sessions():
    registered = set(typer.main.get_group(app).commands)

    listed = ATTACHED_ALLOWED | SESSION_INDEPENDENT | set(ATTACHED_REFUSED)
    assert registered == listed


@pytest.mark.parametrize("command", sorted(ATTACHED_REFUSED))
def test_attached_session_is_ssh_only(
    real_state, tmp_path, monkeypatch, mocker, command
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.py").write_text("print(1)\n")
    real_state.store.add(fresh_session(*ATT))
    real_state.store.add(fresh_session(*CLI))
    contents = mocker.patch("colab_cli.contents.requests.request")
    before = (tmp_path / "sessions.json").read_bytes()

    args = [command, "-s", "att", *ATTACHED_REFUSED[command]]
    result = runner.invoke(app, args, input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    _assert_no_kernel_or_contents_request(real_state, contents)
    assert (tmp_path / "sessions.json").read_bytes() == before


@pytest.mark.parametrize("command", sorted(set(ATTACHED_REFUSED) - {"new", "run"}))
def test_cli_session_on_an_attached_runtime_is_refused(
    real_state, tmp_path, monkeypatch, mocker, command
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.py").write_text("print(1)\n")
    real_state.store.add(fresh_session(*ATT))
    real_state.store.add(fresh_session(*SHARED))
    contents = mocker.patch("colab_cli.contents.requests.request")
    before = (tmp_path / "sessions.json").read_bytes()

    args = [command, "-s", "cli2", *ATTACHED_REFUSED[command]]
    result = runner.invoke(app, args, input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    assert "colab stop -s cli2" in result.output
    _assert_no_kernel_or_contents_request(real_state, contents)
    assert (tmp_path / "sessions.json").read_bytes() == before


def test_refusal_precedes_token_refresh(real_state, tmp_path):
    expired = fresh_session(*ATT)
    expired.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    real_state.store.add(fresh_session(*CLI))
    real_state.store.add(expired)
    before = (tmp_path / "sessions.json").read_bytes()

    result = runner.invoke(app, ["exec", "-s", "att"], input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    assert real_state.client.session.requests == []
    assert (tmp_path / "sessions.json").read_bytes() == before


def test_url_refuses_attached_session(real_state, tmp_path):
    real_state.store.add(fresh_session(*ATT))
    before = (tmp_path / "sessions.json").read_bytes()

    result = runner.invoke(app, ["url", "-s", "att"])

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    assert "dbu=" not in result.output
    assert (tmp_path / "sessions.json").read_bytes() == before


def test_teardown_reads_attached_record_without_ssh_only_refusal(
    real_state, tmp_path, mocker, capsys
):
    real_state.store.add(fresh_session(*ATT))
    use_guarded_client(real_state, mocker)
    before = (tmp_path / "sessions.json").read_bytes()

    run_cmd._teardown("att", fresh_session(*ATT), reason="script_exit")

    assert "colab ssh -s att" not in capsys.readouterr().err
    assert real_state.client.session.requests == []
    run_cmd.ColabRuntime.assert_not_called()
    assert (tmp_path / "sessions.json").read_bytes() == before


PICK_COMMANDS = ("stop", *sorted(set(ATTACHED_REFUSED) - {"new", "run"}))
BOTH_ASSIGNED = ("att-ep", "cli-ep")


def _seed_store(local_state, mocker, records, assigned):
    for record in records:
        local_state.store.add(fresh_session(*record))
    use_guarded_client(local_state, mocker, AssignmentsSession(*assigned))


@pytest.mark.parametrize("command", ["restart-kernel", "url"])
def test_unique_pick_skips_attached_and_shared_records(real_state, mocker, command):
    _seed_store(real_state, mocker, (ATT, SHARED, CLI), BOTH_ASSIGNED)

    result = runner.invoke(app, [command])

    assert result.exit_code == 0, result.output
    assert "Using unique session 'cli'" in result.output
    assert "att-ep" not in result.output


def test_bare_stop_releases_only_the_cli_runtime(real_state, mocker):
    _seed_store(real_state, mocker, (ATT, CLI), BOTH_ASSIGNED)

    result = runner.invoke(app, ["stop"])

    assert result.exit_code == 0, result.output
    assert unassigned_endpoints(real_state) == {"cli-ep"}
    assert real_state.store.get("att").attached is True


@pytest.mark.parametrize("records", [(ATT,), (ATT, ATT2)], ids=["one", "two"])
@pytest.mark.parametrize("command", PICK_COMMANDS)
def test_only_attached_sessions_need_an_explicit_command(
    real_state, tmp_path, monkeypatch, mocker, command, records
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.py").write_text("print(1)\n")
    _seed_store(real_state, mocker, records, ())
    contents = mocker.patch("colab_cli.contents.requests.request")
    before = (tmp_path / "sessions.json").read_bytes()

    args = [command, *ATTACHED_REFUSED.get(command, ())]
    result = runner.invoke(app, args, input="print(1)\n")

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    assert "colab stop -s att" in result.output
    _assert_no_kernel_or_contents_request(real_state, contents)
    assert (tmp_path / "sessions.json").read_bytes() == before


def test_stale_cli_and_attached_session_need_explicit_command(real_state, mocker):
    _seed_store(real_state, mocker, (ATT, CLI), ("att-ep",))

    result = runner.invoke(app, ["restart-kernel"])

    assert result.exit_code == 1, result.output
    assert "colab ssh -s att" in result.output
    assert "colab stop -s att" in result.output
    session_cmd.ColabRuntime.assert_not_called()


def test_bare_ssh_with_cli_and_attached_is_ambiguous(real_state, mocker, capsys):
    _seed_store(real_state, mocker, (ATT, CLI), BOTH_ASSIGNED)

    with pytest.raises(typer.Exit):
        ssh_module._resolve_session(None)

    assert "Multiple active sessions found" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["att", None], ids=["named", "unique"])
def test_ssh_resolves_attached_session(real_state, mocker, name):
    _seed_store(real_state, mocker, (ATT,), ("att-ep",))

    assert ssh_module._resolve_session(name).endpoint == "att-ep"


@pytest.mark.parametrize("command", ["status", "sessions"])
def test_bare_listing_shows_attached(real_state, mocker, command):
    _seed_store(real_state, mocker, (ATT,), ("att-ep",))

    result = runner.invoke(app, [command])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("[att] att-ep |")
