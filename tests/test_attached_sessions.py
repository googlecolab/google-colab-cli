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

from session_fakes import ATT, CLI, fresh_session, use_guarded_client
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import run as run_cmd

runner = CliRunner()


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
