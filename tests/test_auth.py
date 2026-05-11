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

from unittest.mock import MagicMock, mock_open, patch

import pytest

from colab_cli.auth import TOKEN_CONFIG_PATH, AuthProvider, get_credentials


@pytest.fixture
def mock_deps(mocker):
    m_exists = mocker.patch("os.path.exists")
    m_makedirs = mocker.patch("os.makedirs")
    m_creds_cls = mocker.patch("colab_cli.auth.Credentials")
    m_flow_cls = mocker.patch("colab_cli.auth.InstalledAppFlow")
    m_request = mocker.patch("colab_cli.auth.Request")
    m_session = mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    # By default, pretend oauth config doesn't exist
    m_exists.return_value = False

    return {
        "exists": m_exists,
        "makedirs": m_makedirs,
        "creds_cls": m_creds_cls,
        "flow_cls": m_flow_cls,
        "request": m_request,
        "session": m_session,
    }


def test_get_credentials_no_config(mock_deps):
    with pytest.raises(FileNotFoundError, match="Client OAuth config not found"):
        get_credentials("missing_config.json", provider=AuthProvider.OAUTH2)


def test_get_credentials_valid_token(mock_deps):
    # Setup token exists
    def exists_side_effect(path):
        return path in ["dummy_config.json", TOKEN_CONFIG_PATH]

    mock_deps["exists"].side_effect = exists_side_effect

    # Valid creds
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_deps["creds_cls"].from_authorized_user_file.return_value = mock_creds

    # Mock open for config
    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["creds_cls"].from_authorized_user_file.assert_called_once()
    mock_deps["session"].assert_called_once_with(mock_creds)
    assert res == mock_deps["session"].return_value


def test_get_credentials_expired_token_refresh(mock_deps):
    def exists_side_effect(path):
        return path in ["dummy_config.json", TOKEN_CONFIG_PATH]

    mock_deps["exists"].side_effect = exists_side_effect

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "some_token"
    mock_creds.to_json.return_value = '{"token":"refreshed"}'
    mock_deps["creds_cls"].from_authorized_user_file.return_value = mock_creds

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_creds.refresh.assert_called_once()
    m_open.assert_any_call(TOKEN_CONFIG_PATH, "w")
    assert res == mock_deps["session"].return_value


def test_get_credentials_no_token(mock_deps):
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.run_local_server.return_value = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["flow_cls"].from_client_config.assert_called_once()
    mock_flow.run_local_server.assert_called_once()
