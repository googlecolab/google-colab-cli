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

import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def mock_common_state(mocker):
    # Patch the state singleton in common.py
    mock_state = mocker.patch("colab_cli.common.state")

    # Setup standard mocks for properties
    mock_state.store = MagicMock()
    mock_state.client = MagicMock()
    mock_state.history = MagicMock()

    # Default behavior for sync_sessions
    mock_state.sync_sessions.return_value = ({}, [])

    # Global patch for ColabRuntime to prevent network calls
    # We patch it in the modules where it is imported and used
    mocker.patch("colab_cli.commands.session.ColabRuntime")
    mocker.patch("colab_cli.commands.execution.ColabRuntime")
    mocker.patch("colab_cli.commands.automation.ColabRuntime")
    mocker.patch("colab_cli.commands.run.ColabRuntime")

    return mock_state
