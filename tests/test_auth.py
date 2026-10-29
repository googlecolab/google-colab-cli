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

from keyring.errors import KeyringError
import pytest

from colab_cli.auth import (
    KEYRING_SERVICE_NAME,
    KEYRING_USERNAME,
    LEGACY_TOKEN_PATH,
    PUBLIC_SCOPES,
    REMOTE_REDIRECT_URI,
    AuthProvider,
    _run_with_timeout,
    get_credentials,
)


@pytest.fixture
def mock_deps(mocker):
    m_exists = mocker.patch("os.path.exists")
    m_keyring = mocker.patch("colab_cli.auth.keyring", create=True)
    m_creds_cls = mocker.patch("colab_cli.auth.Credentials")
    m_flow_cls = mocker.patch("colab_cli.auth.InstalledAppFlow")
    m_request = mocker.patch("colab_cli.auth.Request")
    m_session = mocker.patch("colab_cli.auth.requests.AuthorizedSession")
    m_resources = mocker.patch("colab_cli.auth.resources")

    # By default, pretend oauth config doesn't exist anywhere and keyring is empty
    m_exists.return_value = False
    m_keyring.get_password.return_value = None
    m_resources.files.return_value.joinpath.return_value.is_file.return_value = False

    return {
        "exists": m_exists,
        "keyring": m_keyring,
        "creds_cls": m_creds_cls,
        "flow_cls": m_flow_cls,
        "request": m_request,
        "session": m_session,
        "resources": m_resources,
    }


def test_get_credentials_no_config(mock_deps):
    with pytest.raises(
        FileNotFoundError,
        match="Client OAuth config not found.*and no inlined config available",
    ):
        get_credentials("missing_config.json", provider=AuthProvider.OAUTH2)


def test_get_credentials_valid_token(mock_deps):
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.return_value = '{"token": "valid"}'

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_deps["creds_cls"].from_authorized_user_info.return_value = mock_creds

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["keyring"].get_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME
    )
    mock_deps["creds_cls"].from_authorized_user_info.assert_called_once_with(
        {"token": "valid"}, PUBLIC_SCOPES
    )
    mock_deps["keyring"].set_password.assert_not_called()
    mock_deps["session"].assert_called_once_with(mock_creds)
    assert res == mock_deps["session"].return_value


def test_get_credentials_expired_token_refresh(mock_deps):
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.return_value = '{"token": "expired"}'

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "some_token"
    mock_creds.to_json.return_value = '{"token":"refreshed"}'
    mock_deps["creds_cls"].from_authorized_user_info.return_value = mock_creds

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_creds.refresh.assert_called_once()
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"refreshed"}'
    )
    assert res == mock_deps["session"].return_value


def test_get_credentials_no_token(mock_deps, mocker):
    """With no cached token, the remote copy-paste flow runs and exchanges code."""
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.return_value = None

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    # User pastes the authorization code at the prompt.
    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["flow_cls"].from_client_config.assert_called_once()
    # No localhost server should ever be started.
    mock_flow.run_local_server.assert_not_called()
    # Remote flow: OOB-free redirect + token_usage=remote consent param.
    assert mock_flow.redirect_uri == REMOTE_REDIRECT_URI
    _, kwargs = mock_flow.authorization_url.call_args
    assert kwargs.get("token_usage") == "remote"
    # The pasted code is exchanged for a token.
    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"new"}'
    )


def test_get_credentials_keyring_inaccessible(mock_deps, mocker):
    """When keyring is inaccessible, invoke _run_remote_flow and return credentials."""
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.side_effect = KeyringError(
        "No recommended backend was available"
    )
    mock_deps["keyring"].set_password.side_effect = KeyringError(
        "No recommended backend was available"
    )

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["keyring"].get_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME
    )
    mock_deps["flow_cls"].from_client_config.assert_called_once()
    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"new"}'
    )
    mock_deps["session"].assert_called_once_with(mock_creds_new)
    assert res == mock_deps["session"].return_value


def test_get_credentials_corrupt_keyring_data(mock_deps, mocker):
    """When keyring contains corrupt JSON, fall back to _run_remote_flow and overwrite."""
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.return_value = "not-valid-json"

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"new"}'
    )
    assert res == mock_deps["session"].return_value


def test_get_credentials_refresh_failure_falls_back_to_remote_flow(mock_deps, mocker):
    """When refreshing an expired token fails, fall back to _run_remote_flow."""
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.return_value = '{"token": "expired"}'

    mock_creds_expired = MagicMock()
    mock_creds_expired.valid = False
    mock_creds_expired.expired = True
    mock_creds_expired.refresh_token = "bad_refresh_token"
    mock_creds_expired.refresh.side_effect = Exception("Token revoked")
    mock_deps["creds_cls"].from_authorized_user_info.return_value = mock_creds_expired

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_creds_expired.refresh.assert_called_once()
    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"new"}'
    )
    mock_deps["session"].assert_called_once_with(mock_creds_new)
    assert res == mock_deps["session"].return_value


def test_get_credentials_keyring_timeout_falls_back_to_remote_flow(mock_deps, mocker):
    """When a keyring call hangs beyond the timeout, fall back to _run_remote_flow."""
    import threading

    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mocker.patch("colab_cli.auth.KEYRING_TIMEOUT_SECONDS", 0.05)

    hang_event = threading.Event()
    mock_deps["keyring"].get_password.side_effect = lambda *a: hang_event.wait(1.0)
    mock_deps["keyring"].set_password.side_effect = lambda *a: hang_event.wait(1.0)

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    try:
        with patch("builtins.open", m_open):
            res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)
    finally:
        hang_event.set()

    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    mock_deps["session"].assert_called_once_with(mock_creds_new)
    assert res == mock_deps["session"].return_value


def _setup_remote_flow(mock_deps, mocker):
    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow
    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")
    return mock_flow, mock_creds_new


def test_keyring_read_failure_warns_on_stderr(mock_deps, mocker, capsys):
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].get_password.side_effect = KeyringError("locked")
    _setup_remote_flow(mock_deps, mocker)

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    err = capsys.readouterr().err
    assert "Could not read the OAuth token from the OS keyring" in err
    assert "locked" in err


def test_keyring_save_failure_warns_on_stderr(mock_deps, mocker, capsys):
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    mock_deps["keyring"].set_password.side_effect = KeyringError("no backend")
    _setup_remote_flow(mock_deps, mocker)

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    err = capsys.readouterr().err
    assert "Could not save the OAuth token to the OS keyring" in err
    assert "no backend" in err
    assert "--auth=adc" in err


def test_legacy_token_file_migrated_to_keyring(mock_deps, mocker):
    """A pre-keyring token.json is used, copied into the keyring, then deleted."""
    mock_deps["exists"].side_effect = lambda path: path in (
        "dummy_config.json",
        LEGACY_TOKEN_PATH,
    )
    m_remove = mocker.patch("colab_cli.auth.os.remove")

    legacy_creds = MagicMock()
    legacy_creds.valid = True
    legacy_creds.to_json.return_value = '{"token":"legacy"}'
    mock_deps["creds_cls"].from_authorized_user_file.return_value = legacy_creds

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["creds_cls"].from_authorized_user_file.assert_called_once_with(
        LEGACY_TOKEN_PATH, PUBLIC_SCOPES
    )
    mock_deps["flow_cls"].from_client_config.assert_not_called()
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"legacy"}'
    )
    m_remove.assert_called_once_with(LEGACY_TOKEN_PATH)
    mock_deps["session"].assert_called_once_with(legacy_creds)
    assert res == mock_deps["session"].return_value


def test_legacy_token_file_kept_when_keyring_save_fails(mock_deps, mocker):
    """Never delete the legacy token unless it was persisted to the keyring."""
    mock_deps["exists"].side_effect = lambda path: path in (
        "dummy_config.json",
        LEGACY_TOKEN_PATH,
    )
    mock_deps["keyring"].get_password.side_effect = KeyringError("locked")
    mock_deps["keyring"].set_password.side_effect = KeyringError("locked")
    m_remove = mocker.patch("colab_cli.auth.os.remove")

    legacy_creds = MagicMock()
    legacy_creds.valid = True
    legacy_creds.to_json.return_value = '{"token":"legacy"}'
    mock_deps["creds_cls"].from_authorized_user_file.return_value = legacy_creds

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["flow_cls"].from_client_config.assert_not_called()
    m_remove.assert_not_called()
    mock_deps["session"].assert_called_once_with(legacy_creds)


def test_legacy_token_file_deleted_when_keyring_has_token(mock_deps, mocker):
    mock_deps["exists"].side_effect = lambda path: path in (
        "dummy_config.json",
        LEGACY_TOKEN_PATH,
    )
    m_remove = mocker.patch("colab_cli.auth.os.remove")
    mock_deps["keyring"].get_password.return_value = '{"token": "valid"}'
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_deps["creds_cls"].from_authorized_user_info.return_value = mock_creds

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["creds_cls"].from_authorized_user_file.assert_not_called()
    mock_deps["keyring"].set_password.assert_not_called()
    m_remove.assert_called_once_with(LEGACY_TOKEN_PATH)
    mock_deps["session"].assert_called_once_with(mock_creds)


def test_corrupt_legacy_token_file_falls_back_to_remote_flow(mock_deps, mocker):
    """A corrupt legacy token is deleted right away, then the remote flow runs."""
    files = {"dummy_config.json", LEGACY_TOKEN_PATH}
    mock_deps["exists"].side_effect = lambda path: path in files
    m_remove = mocker.patch(
        "colab_cli.auth.os.remove", side_effect=lambda path: files.discard(path)
    )
    mock_deps["creds_cls"].from_authorized_user_file.side_effect = ValueError("bad")
    mock_flow, mock_creds_new = _setup_remote_flow(mock_deps, mocker)

    legacy_present_at_prompt = []
    mocker.patch(
        "colab_cli.auth.input",
        create=True,
        side_effect=lambda _: legacy_present_at_prompt.append(
            LEGACY_TOKEN_PATH in files
        )
        or "pasted-code",
    )

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    assert legacy_present_at_prompt == [False]
    m_remove.assert_called_once_with(LEGACY_TOKEN_PATH)
    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    mock_deps["keyring"].set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, KEYRING_USERNAME, '{"token":"new"}'
    )


def test_corrupt_legacy_token_file_deleted_even_if_keyring_save_fails(
    mock_deps, mocker
):
    """The corrupt file holds nothing worth keeping, unlike a valid legacy token."""
    mock_deps["exists"].side_effect = lambda path: path in (
        "dummy_config.json",
        LEGACY_TOKEN_PATH,
    )
    mock_deps["keyring"].set_password.side_effect = KeyringError("locked")
    m_remove = mocker.patch("colab_cli.auth.os.remove")
    mock_deps["creds_cls"].from_authorized_user_file.side_effect = ValueError("bad")
    _setup_remote_flow(mock_deps, mocker)

    with patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    m_remove.assert_called_once_with(LEGACY_TOKEN_PATH)


def test_run_with_timeout_infers_operation_name():
    """TimeoutError message should include the inferred callable name."""
    import threading

    hang_event = threading.Event()

    def custom_slow_operation():
        hang_event.wait(1.0)

    try:
        with pytest.raises(
            TimeoutError,
            match="Operation 'custom_slow_operation' timed out after 0.0s",
        ):
            _run_with_timeout(custom_slow_operation, timeout=0.01)
    finally:
        hang_event.set()


def test_remote_redirect_is_not_oob():
    """Guard against regressing to the dead OOB redirect URI."""
    assert REMOTE_REDIRECT_URI.startswith("https://")
    assert "oob" not in REMOTE_REDIRECT_URI


def test_get_credentials_fallback_config(mock_deps):
    # Setup: config_path doesn't exist, but fallback file does
    mock_deps["exists"].return_value = False
    m_file = mock_deps["resources"].files.return_value.joinpath.return_value
    m_file.is_file.return_value = True
    m_file.read_text.return_value = '{"installed":{"client_id":"fallback_id"}}'

    # Valid creds in keyring
    mock_deps["keyring"].get_password.return_value = '{"token": "valid"}'
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_deps["creds_cls"].from_authorized_user_info.return_value = mock_creds

    res = get_credentials("missing_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["resources"].files.assert_called_once_with("colab_cli")
    m_file.is_file.assert_called_once()
    m_file.read_text.assert_called_once()
    mock_deps["creds_cls"].from_authorized_user_info.assert_called_once()
    assert res == mock_deps["session"].return_value
