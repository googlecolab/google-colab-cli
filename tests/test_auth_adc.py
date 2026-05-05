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

"""Tests for the Application Default Credentials (ADC) auth provider."""

from unittest.mock import MagicMock

import pytest

from colab_cli.auth import AuthProvider, get_credentials


def test_get_credentials_adc_success(mocker):
    """`--auth=adc` should delegate to google.auth.default() and wrap the
    resulting credentials in an AuthorizedSession."""
    mock_creds = MagicMock()
    # Default ADC user creds don't need (or support) re-scoping; the scopes
    # are fixed at `gcloud auth application-default login` time.
    mock_creds.requires_scopes = False
    mock_default = mocker.patch(
        "google.auth.default", return_value=(mock_creds, "some-project-id")
    )
    mock_session_cls = mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    res = get_credentials(provider=AuthProvider.ADC)

    mock_default.assert_called_once()
    mock_session_cls.assert_called_once_with(mock_creds)
    assert res == mock_session_cls.return_value


def test_get_credentials_adc_default_error_propagates(mocker):
    """If google.auth.default() raises (e.g. no ADC configured), the error
    should propagate to the caller so they can run `gcloud auth
    application-default login`."""
    from google.auth.exceptions import DefaultCredentialsError

    mocker.patch(
        "google.auth.default",
        side_effect=DefaultCredentialsError("No ADC found"),
    )
    mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    with pytest.raises(DefaultCredentialsError):
        get_credentials(provider=AuthProvider.ADC)


def test_get_credentials_adc_does_not_invoke_other_providers(mocker):
    """ADC path must not shell out to gcert/stubby or kick off the
    InstalledAppFlow."""
    mock_creds = MagicMock()
    mock_creds.requires_scopes = False
    mocker.patch("google.auth.default", return_value=(mock_creds, None))
    mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    mock_subprocess = mocker.patch("subprocess.run")
    mock_flow = mocker.patch("colab_cli.auth.InstalledAppFlow")

    get_credentials(provider=AuthProvider.ADC)

    mock_subprocess.assert_not_called()
    mock_flow.from_client_config.assert_not_called()


def test_get_credentials_adc_requests_colaboratory_scope(mocker):
    """The Boq RuntimeService at colab.pa.googleapis.com requires the
    `colaboratory` scope. ADC must request it via google.auth.default().
    """
    mock_creds = MagicMock()
    # Pretend creds don't need re-scoping (e.g., user creds from gcloud).
    mock_creds.requires_scopes = False
    mock_default = mocker.patch(
        "google.auth.default", return_value=(mock_creds, "proj")
    )
    mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    get_credentials(provider=AuthProvider.ADC)

    scopes = mock_default.call_args.kwargs.get("scopes")
    assert scopes is not None, "google.auth.default() must be called with scopes="
    assert "https://www.googleapis.com/auth/colaboratory" in scopes
    assert "https://www.googleapis.com/auth/userinfo.email" in scopes


def test_get_credentials_adc_reapplies_scopes_for_scopable_creds(mocker):
    """For credential subclasses that support `with_scopes` (service accounts,
    GCE/GKE, etc.), we must call it so the colaboratory scope sticks even if
    google.auth.default() ignored the kwarg.
    """
    rescoped = MagicMock(name="rescoped_creds")
    mock_creds = MagicMock()
    mock_creds.requires_scopes = True
    mock_creds.with_scopes.return_value = rescoped
    mocker.patch("google.auth.default", return_value=(mock_creds, "proj"))
    mock_session_cls = mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    get_credentials(provider=AuthProvider.ADC)

    mock_creds.with_scopes.assert_called_once()
    applied_scopes = mock_creds.with_scopes.call_args.args[0]
    assert "https://www.googleapis.com/auth/colaboratory" in applied_scopes
    # The session is built from the *rescoped* creds, not the original.
    mock_session_cls.assert_called_once_with(rescoped)


def test_get_credentials_adc_tolerates_with_scopes_failure(mocker):
    """User creds (from `gcloud auth application-default login`) raise
    NotImplementedError on with_scopes. We must fall back gracefully."""
    mock_creds = MagicMock()
    mock_creds.requires_scopes = True
    mock_creds.with_scopes.side_effect = NotImplementedError("user creds")
    mocker.patch("google.auth.default", return_value=(mock_creds, "proj"))
    mock_session_cls = mocker.patch("colab_cli.auth.requests.AuthorizedSession")

    # Should not raise.
    get_credentials(provider=AuthProvider.ADC)

    # Falls back to using the original (un-rescoped) creds.
    mock_session_cls.assert_called_once_with(mock_creds)
