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

from importlib.metadata import PackageNotFoundError
from typer.testing import CliRunner
from unittest.mock import patch

from colab_cli.cli import app

runner = CliRunner()


def test_version_installed():
    with patch("colab_cli.auto_update.installed_version") as mock_version:
        mock_version.return_value = "0.2.0"
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "Version: 0.2.0" in result.output


def test_version_git_fallback():
    with patch("colab_cli.auto_update.installed_version") as mock_version:
        mock_version.side_effect = PackageNotFoundError

        with patch("subprocess.check_output") as mock_git:
            mock_git.return_value = "abc1234"
            result = runner.invoke(app, ["version"])
            assert result.exit_code == 0
            assert "Version: abc1234" in result.output


def test_version_unknown():
    with patch("colab_cli.auto_update.installed_version") as mock_version:
        mock_version.side_effect = PackageNotFoundError

        with patch("subprocess.check_output") as mock_git:
            mock_git.side_effect = Exception("git not found")
            result = runner.invoke(app, ["version"])
            assert result.exit_code == 0
            assert "Version: unknown" in result.output
