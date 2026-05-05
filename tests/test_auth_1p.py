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

from unittest.mock import MagicMock

import pytest

from colab_cli.auth import AuthProvider, get_credentials


def test_get_credentials_1p_success(mocker):
    # Mock subprocess.run
    mock_run = mocker.patch("subprocess.run")

    # 1. gcertstatus check (success)
    mock_gcert_check = MagicMock()
    mock_gcert_check.returncode = 0

    # 2. stubby call (success)
    mock_stubby_result = MagicMock()
    mock_stubby_result.stdout = 'oauth2_token: "mock_token_123"'

    mock_run.side_effect = [mock_gcert_check, mock_stubby_result]

    # Mock AuthorizedSession
    mock_session_cls = mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    res = get_credentials()

    # Verify gcertstatus was checked
    assert mock_run.call_args_list[0][0][0] == [
        "gcertstatus",
        "--check_remaining=5m",
        "--quiet",
    ]

    # Verify stubby was called correctly
    assert mock_run.call_count == 2
    args, kwargs = mock_run.call_args_list[1]
    assert "stubby" in args[0]
    assert "https://www.googleapis.com/auth/userinfo.email" in kwargs["input"]

    # Verify session was created
    mock_session_cls.assert_called_once()
    assert res == mock_session_cls.return_value


def test_get_credentials_1p_does_not_request_colaboratory_scope(mocker):
    """The corp LOAS-to-OAuth exchange policy
    (security/corplogin/exchange/policy/policy.ncl) does NOT allowlist the
    `colaboratory` scope, so requesting it returns "Your request did not
    match any of the authorized CXF policy rules" and we get no token at all.

    Pin that the LOAS2 path requests ONLY `userinfo.email`. Until the policy
    exception lands (go/enterprise-identity-intake), adding `colaboratory`
    here would break `colab new` outright. Keep-alive is intentionally
    disabled for this provider in commands/session.py instead.
    """
    mock_run = mocker.patch("subprocess.run")

    mock_gcert_check = MagicMock()
    mock_gcert_check.returncode = 0

    mock_stubby_result = MagicMock()
    mock_stubby_result.stdout = 'oauth2_token: "tok"'

    mock_run.side_effect = [mock_gcert_check, mock_stubby_result]
    mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    get_credentials()

    stubby_input = mock_run.call_args_list[1].kwargs["input"]
    assert "https://www.googleapis.com/auth/userinfo.email" in stubby_input
    # The corplogin policy rejects this scope; do not request it.
    assert "https://www.googleapis.com/auth/colaboratory" not in stubby_input


def test_get_credentials_1p_gcert_fallback(mocker):
    # gcertstatus fails, gcert succeeds, stubby succeeds
    mock_run = mocker.patch("subprocess.run")
    mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    # 1. gcertstatus check (fail)
    mock_gcert_check = MagicMock()
    mock_gcert_check.returncode = 1

    # 2. gcert run (success)
    mock_gcert_run = MagicMock()

    # 3. stubby call (success)
    mock_stubby_result = MagicMock()
    mock_stubby_result.stdout = 'oauth2_token: "token_after_gcert"'

    mock_run.side_effect = [mock_gcert_check, mock_gcert_run, mock_stubby_result]

    get_credentials()

    assert mock_run.call_count == 3
    assert mock_run.call_args_list[0][0][0] == [
        "gcertstatus",
        "--check_remaining=5m",
        "--quiet",
    ]
    assert mock_run.call_args_list[1][0][0] == ["gcert"]
    assert "stubby" in mock_run.call_args_list[2][0][0]


def test_get_credentials_1p_failure(mocker):
    # gcertstatus succeeds, but stubby fails
    mock_run = mocker.patch("subprocess.run")

    mock_gcert_check = MagicMock()
    mock_gcert_check.returncode = 0

    mock_stubby_result = MagicMock()
    mock_stubby_result.stdout = "failure"

    mock_run.side_effect = [mock_gcert_check, mock_stubby_result]

    with pytest.raises(SystemExit) as excinfo:
        get_credentials()

    assert excinfo.value.code == 1


def test_get_credentials_public_success(mocker):
    # Mock _get_google_auth_credentials
    mock_get_public = mocker.patch("colab_cli.auth._get_google_auth_credentials")
    mock_creds = MagicMock()
    mock_get_public.return_value = mock_creds

    # Mock AuthorizedSession
    mock_session_cls = mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    res = get_credentials(provider=AuthProvider.OAUTH2)

    # Verify public flow was called
    mock_get_public.assert_called_once()
    mock_session_cls.assert_called_once_with(mock_creds)
    assert res == mock_session_cls.return_value
