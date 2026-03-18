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
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


@pytest.fixture
def mock_settings(tmp_path):
    settings_path = tmp_path / "settings.json"
    with patch("colab_cli.state.SettingsStore.__init__", return_value=None):
        with patch("colab_cli.state.SettingsStore.path", settings_path):
            yield settings_path


def test_update_command_no_update(mocker):
    # Mock get_app_version to return "1.0.0"
    mocker.patch("colab_cli.commands.utility.get_app_version", return_value="1.0.0")

    # Mock urllib.request.urlopen
    mock_urlopen = mocker.patch("urllib.request.urlopen")
    mock_response = mock_urlopen.return_value.__enter__.return_value
    mock_response.read.return_value = json.dumps({"info": {"version": "1.0.0"}}).encode(
        "utf-8"
    )

    # Mock settings store
    from colab_cli.state import Settings

    mock_settings = Settings(update_url="http://test.url", last_check=None)
    mocker.patch("colab_cli.state.SettingsStore.load", return_value=mock_settings)
    mocker.patch("colab_cli.state.SettingsStore.save")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "Colab CLI is up to date (version: 1.0.0)" in result.output


def test_update_command_with_update(mocker):
    # Mock get_app_version to return "1.0.0"
    mocker.patch("colab_cli.commands.utility.get_app_version", return_value="1.0.0")

    # Mock urllib.request.urlopen
    mock_urlopen = mocker.patch("urllib.request.urlopen")
    mock_response = mock_urlopen.return_value.__enter__.return_value
    mock_response.read.return_value = json.dumps({"info": {"version": "1.1.0"}}).encode(
        "utf-8"
    )

    # Mock settings store
    from colab_cli.state import Settings

    mock_settings = Settings(update_url="http://test.url", last_check=None)
    mocker.patch("colab_cli.state.SettingsStore.load", return_value=mock_settings)
    mocker.patch("colab_cli.state.SettingsStore.save")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert (
        "A new version of Colab CLI is available: 1.1.0 (current: 1.0.0)"
        in result.output
    )
    assert "Run 'pip install --upgrade colab' to update." in result.output


def test_update_command_older_version(mocker):
    # Mock get_app_version to return "1.1.0"
    mocker.patch("colab_cli.commands.utility.get_app_version", return_value="1.1.0")

    # Mock urllib.request.urlopen to return an older version "1.0.0"
    mock_urlopen = mocker.patch("urllib.request.urlopen")
    mock_response = mock_urlopen.return_value.__enter__.return_value
    mock_response.read.return_value = json.dumps({"info": {"version": "1.0.0"}}).encode(
        "utf-8"
    )

    # Mock settings store
    from colab_cli.state import Settings

    mock_settings = Settings(update_url="http://test.url", last_check=None)
    mocker.patch("colab_cli.state.SettingsStore.load", return_value=mock_settings)
    mocker.patch("colab_cli.state.SettingsStore.save")

    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "Colab CLI is up to date (version: 1.1.0)" in result.output


def test_auto_update_trigger(mocker):
    # Mock get_app_version
    mocker.patch("colab_cli.commands.utility.get_app_version", return_value="1.0.0")

    # Mock urllib.request.urlopen
    mock_urlopen = mocker.patch("urllib.request.urlopen")
    mock_response = mock_urlopen.return_value.__enter__.return_value
    mock_response.read.return_value = json.dumps({"info": {"version": "1.1.0"}}).encode(
        "utf-8"
    )

    # Mock settings store to have a last_check more than 1 day ago
    last_check = datetime.now(timezone.utc) - timedelta(days=2)
    from colab_cli.state import Settings

    mock_settings = Settings(update_url="http://test.url", last_check=last_check)

    mocker.patch("colab_cli.state.SettingsStore.load", return_value=mock_settings)
    mocker.patch("colab_cli.state.SettingsStore.save")

    # Invoke any command except 'update'
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    # Should show the update message because it was triggered automatically
    assert (
        "A new version of Colab CLI is available: 1.1.0 (current: 1.0.0)"
        in result.output
    )


def test_auto_update_no_trigger_recent(mocker):
    # Mock settings store to have a last_check very recent
    last_check = datetime.now(timezone.utc) - timedelta(hours=1)
    from colab_cli.state import Settings

    mock_settings = Settings(update_url="http://test.url", last_check=last_check)
    mocker.patch("colab_cli.state.SettingsStore.load", return_value=mock_settings)

    # Mock check_for_updates to verify it's NOT called
    mock_check = mocker.patch("colab_cli.commands.utility.check_for_updates")

    runner.invoke(app, ["version"])
    assert mock_check.call_count == 0


def test_auto_update_first_time(mocker):
    # Mock settings store to have last_check as None
    from colab_cli.state import Settings

    mock_settings = Settings(
        update_url="http://test.url", last_check=None, enable_auto_update=True
    )

    mocker.patch("colab_cli.state.SettingsStore.load", return_value=mock_settings)
    mocker.patch("colab_cli.state.SettingsStore.save")
    mocker.patch("colab_cli.commands.utility.get_app_version", return_value="1.0.0")

    # Mock check_for_updates to avoid network calls
    mock_check = mocker.patch("colab_cli.commands.utility.check_for_updates")

    # Invoke any command except 'update'
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert mock_check.call_count == 1
