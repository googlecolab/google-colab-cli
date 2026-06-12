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

from unittest.mock import patch, mock_open, MagicMock
from typer.testing import CliRunner
import pytest

from colab_cli.cli import app

runner = CliRunner()


@pytest.fixture
def mock_resources():
    with patch("importlib.resources.files") as mock:
        yield mock


def test_readme_from_resources(mock_resources):
    mock_readme = MagicMock()
    mock_readme.is_file.return_value = True
    mock_readme.read_text.return_value = "Fake README content"

    def joinpath_side_effect(name):
        if name == "README.md":
            return mock_readme
        return MagicMock(is_file=MagicMock(return_value=False))

    mock_resources.return_value.joinpath.side_effect = joinpath_side_effect

    result = runner.invoke(app, ["README"])
    assert result.exit_code == 0
    assert result.output.strip() == "Fake README content"
    mock_resources.assert_called_once_with("colab_cli")
    mock_resources.return_value.joinpath.assert_called_with("README.md")


def test_skill_from_resources(mock_resources):
    mock_skill = MagicMock()
    mock_skill.is_file.return_value = True
    mock_skill.read_text.return_value = "Fake SKILL content"

    def joinpath_side_effect(name):
        if name == "SKILL.md":
            return mock_skill
        return MagicMock(is_file=MagicMock(return_value=False))

    mock_resources.return_value.joinpath.side_effect = joinpath_side_effect

    result = runner.invoke(app, ["SKILL"])
    assert result.exit_code == 0
    assert result.output.strip() == "Fake SKILL content"
    mock_resources.assert_called_once_with("colab_cli")
    mock_resources.return_value.joinpath.assert_called_with("SKILL.md")


def test_readme_fallback_to_file(mock_resources):
    mock_resources.side_effect = Exception("No resources")

    import builtins

    real_open = builtins.open

    def mock_open_impl(file, *args, **kwargs):
        if "README.md" in str(file):
            return mock_open(read_data="Fake local README")(*args, **kwargs)
        return real_open(file, *args, **kwargs)

    with patch("os.path.exists", return_value=True):
        with patch("builtins.open", side_effect=mock_open_impl):
            result = runner.invoke(app, ["README"])
            assert result.exit_code == 0
            assert result.output.strip() == "Fake local README"


def test_readme_failure(mock_resources):
    mock_resources.side_effect = Exception("No resources")
    with patch("os.path.exists", return_value=False):
        result = runner.invoke(app, ["README"])
        assert result.exit_code == 1
        assert "README.md content not available" in result.output
