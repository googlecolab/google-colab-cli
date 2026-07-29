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
import os
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.auth import (
    REMOTE_REDIRECT_URI,
    complete_remote_flow,
    start_remote_flow,
)
from colab_cli.cli import app

runner = CliRunner()


@pytest.fixture
def mock_deps(mocker, tmp_path):
    mocker.patch("colab_cli.auth.LOGIN_LOCK_PATH", str(tmp_path / "login.lock.json"))
    mocker.patch("colab_cli.auth.TOKEN_CONFIG_PATH", str(tmp_path / "token.json"))
    mocker.patch("colab_cli.auth.InstalledAppFlow")
    mocker.patch("colab_cli.auth._load_client_config", return_value={"web": {"client_id": "test-id"}})
    return tmp_path


def test_start_remote_flow_creates_lock(mock_deps, mocker):
    mock_flow = MagicMock()
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state123")
    mocker.patch(
        "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
    )

    auth_url, lock_data = start_remote_flow("dummy_config.json")

    assert auth_url == "https://auth.example/url"
    assert lock_data["state"] == "state123"
    assert lock_data["redirect_uri"] == REMOTE_REDIRECT_URI
    assert "client_config" not in lock_data
    assert os.path.exists(str(mock_deps / "login.lock.json"))


def test_start_remote_flow_fallback_config(mock_deps, mocker):
    mock_flow = MagicMock()
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state456")
    mocker.patch(
        "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
    )

    auth_url, lock_data = start_remote_flow("missing_config.json")

    assert auth_url == "https://auth.example/url"
    assert lock_data["state"] == "state456"
    assert os.path.exists(str(mock_deps / "login.lock.json"))


def test_start_remote_flow_no_config(mock_deps, mocker):
    mocker.patch("colab_cli.auth._load_client_config", side_effect=FileNotFoundError("Client OAuth config not found"))

    with pytest.raises(FileNotFoundError, match="Client OAuth config not found"):
        start_remote_flow("missing_config.json")


def test_complete_remote_flow_exchanges_code(mock_deps, mocker):
    lock_data = {
        "state": "state123",
        "redirect_uri": REMOTE_REDIRECT_URI,
        "scopes": ["openid"],
    }
    lock_path = str(mock_deps / "login.lock.json")
    with open(lock_path, "w") as f:
        json.dump(lock_data, f)

    mock_flow = MagicMock()
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token":"new-token"}'
    mock_flow.credentials = mock_creds
    mocker.patch(
        "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
    )

    complete_remote_flow(lock_path, "pasted-code", "dummy_config.json")

    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")
    assert mock_flow.state == "state123"
    assert not os.path.exists(lock_path)
    assert os.path.exists(str(mock_deps / "token.json"))


def test_complete_remote_flow_no_lock(mock_deps):
    with pytest.raises(FileNotFoundError, match="No pending login session found"):
        complete_remote_flow(str(mock_deps / "nonexistent.lock.json"), "code", "dummy_config.json")


def test_complete_remote_flow_cleanup_on_failure(mock_deps, mocker):
    lock_data = {
        "state": "state123",
        "redirect_uri": REMOTE_REDIRECT_URI,
        "scopes": ["openid"],
    }
    lock_path = str(mock_deps / "login.lock.json")
    with open(lock_path, "w") as f:
        json.dump(lock_data, f)

    mock_flow = MagicMock()
    mock_flow.fetch_token.side_effect = Exception("network error")
    mocker.patch(
        "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
    )

    with pytest.raises(Exception, match="network error"):
        complete_remote_flow(lock_path, "bad-code", "dummy_config.json")

    assert not os.path.exists(lock_path)


def test_complete_remote_flow_state_validation(mock_deps, mocker):
    lock_data = {
        "state": "state123",
        "redirect_uri": REMOTE_REDIRECT_URI,
        "scopes": ["openid"],
    }
    lock_path = str(mock_deps / "login.lock.json")
    with open(lock_path, "w") as f:
        json.dump(lock_data, f)

    mock_flow = MagicMock()
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token":"ok"}'
    mock_flow.credentials = mock_creds
    mocker.patch(
        "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
    )

    with pytest.raises(FileNotFoundError, match="State mismatch"):
        complete_remote_flow(lock_path, "code", "dummy_config.json", lock_data=lock_data, state="wrong-state")

    mock_flow.fetch_token.assert_not_called()


def test_complete_remote_flow_missing_keys_cleans_lock(mock_deps, mocker):
    lock_data = {
        "state": "state123",
        "redirect_uri": REMOTE_REDIRECT_URI,
    }
    lock_path = str(mock_deps / "login.lock.json")
    with open(lock_path, "w") as f:
        json.dump(lock_data, f)

    mocker.patch(
        "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=MagicMock()
    )

    with pytest.raises(FileNotFoundError, match="missing required keys"):
        complete_remote_flow(lock_path, "code", "dummy_config.json", lock_data=lock_data)

    assert not os.path.exists(lock_path)


class TestLoginCLI:
    def test_login_start_creates_lock_and_prints_url(self, mocker, tmp_path):
        lock_path = str(tmp_path / "login.lock.json")
        mocker.patch("colab_cli.commands.login.LOGIN_LOCK_PATH", lock_path)

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"web": {"client_id": "test-id"}}))

        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = (
            "https://auth.example/login",
            "state999",
        )
        mocker.patch(
            "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
        )

        result = runner.invoke(app, ["login", "start", "-c", str(config_path)])
        assert result.exit_code == 0
        assert "https://auth.example/login" in result.output
        assert "Step 1 of 2" in result.output
        assert "colab login verify" in result.output

    def test_login_verify_success(self, mocker, tmp_path):
        lock_path = str(tmp_path / "login.lock.json")
        mocker.patch("colab_cli.commands.login.LOGIN_LOCK_PATH", lock_path)
        mocker.patch(
            "colab_cli.commands.login.TOKEN_CONFIG_PATH", str(tmp_path / "token.json")
        )

        lock_data = {
            "state": "state123",
            "redirect_uri": REMOTE_REDIRECT_URI,
            "scopes": ["openid"],
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"token":"ok"}'
        mock_flow.credentials = mock_creds
        mocker.patch(
            "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
        )

        result = runner.invoke(app, ["login", "verify", "auth-code"])
        assert result.exit_code == 0
        assert "Login complete" in result.output
        assert not os.path.exists(lock_path)

    def test_login_verify_success_from_url(self, mocker, tmp_path):
        lock_path = str(tmp_path / "login.lock.json")
        mocker.patch("colab_cli.commands.login.LOGIN_LOCK_PATH", lock_path)
        mocker.patch(
            "colab_cli.commands.login.TOKEN_CONFIG_PATH", str(tmp_path / "token.json")
        )

        lock_data = {
            "state": "state123",
            "redirect_uri": REMOTE_REDIRECT_URI,
            "scopes": ["openid"],
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"token":"ok"}'
        mock_flow.credentials = mock_creds
        mocker.patch(
            "colab_cli.auth.InstalledAppFlow.from_client_config", return_value=mock_flow
        )

        redirect_url = (
            "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
            "?state=state123"
            "&code=auth-code"
        )
        result = runner.invoke(app, ["login", "verify", redirect_url])
        assert result.exit_code == 0
        assert "Login complete" in result.output
        assert not os.path.exists(lock_path)

    def test_login_verify_no_lock_file(self, mocker, tmp_path):
        mocker.patch(
            "colab_cli.commands.login.LOGIN_LOCK_PATH",
            str(tmp_path / "nonexistent.lock.json"),
        )

        redirect_url = (
            "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
            "?state=state123"
            "&code=auth-code"
        )
        result = runner.invoke(app, ["login", "verify", redirect_url])
        assert result.exit_code == 1
        assert "No pending login session found" in result.output

    def test_login_verify_state_mismatch_cleans_lock(self, mocker, tmp_path):
        lock_path = str(tmp_path / "login.lock.json")
        mocker.patch("colab_cli.commands.login.LOGIN_LOCK_PATH", lock_path)
        mocker.patch(
            "colab_cli.commands.login.TOKEN_CONFIG_PATH", str(tmp_path / "token.json")
        )

        lock_data = {
            "state": "state123",
            "redirect_uri": REMOTE_REDIRECT_URI,
            "scopes": ["openid"],
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        redirect_url = (
            "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
            "?state=wrong-state"
            "&code=auth-code"
        )
        result = runner.invoke(app, ["login", "verify", redirect_url])
        assert result.exit_code == 1
        assert "State mismatch" in result.output
        assert not os.path.exists(lock_path)

    def test_login_verify_url_without_code_fails(self, mocker, tmp_path):
        mocker.patch(
            "colab_cli.commands.login.LOGIN_LOCK_PATH",
            str(tmp_path / "login.lock.json"),
        )

        redirect_url = "https://sdk.cloud.google.com/applicationdefaultauthcode.html?state=state123"
        result = runner.invoke(app, ["login", "verify", redirect_url])
        assert result.exit_code == 1
        assert "Could not extract authorization code from URL" in result.output

    def test_login_verify_url_without_state_fails(self, mocker, tmp_path):
        mocker.patch(
            "colab_cli.commands.login.LOGIN_LOCK_PATH",
            str(tmp_path / "login.lock.json"),
        )

        redirect_url = "https://sdk.cloud.google.com/applicationdefaultauthcode.html?code=auth-code"
        result = runner.invoke(app, ["login", "verify", redirect_url])
        assert result.exit_code == 1
        assert "Could not extract state from URL" in result.output

    def test_login_verify_corrupted_lock_file(self, mocker, tmp_path):
        lock_path = str(tmp_path / "login.lock.json")
        mocker.patch("colab_cli.commands.login.LOGIN_LOCK_PATH", lock_path)

        with open(lock_path, "w") as f:
            f.write("not valid json{")

        redirect_url = (
            "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
            "?state=state123"
            "&code=auth-code"
        )
        result = runner.invoke(app, ["login", "verify", redirect_url])
        assert result.exit_code == 1
        assert "Lock file is corrupted" in result.output
        assert not os.path.exists(lock_path)


def test_is_lock_expired_missing_created_at():
    from colab_cli.auth import _is_lock_expired
    
    lock_data = {"state": "state123"}
    assert not _is_lock_expired(lock_data)


def test_is_lock_expired_valid_timestamp():
    from colab_cli.auth import _is_lock_expired
    import datetime
    
    lock_data = {"created_at": datetime.datetime.now().isoformat()}
    assert not _is_lock_expired(lock_data)


def test_is_lock_expired_old_timestamp():
    from colab_cli.auth import _is_lock_expired
    
    lock_data = {"created_at": "2020-01-01T00:00:00"}
    assert _is_lock_expired(lock_data)


def test_is_lock_expired_invalid_timestamp():
    from colab_cli.auth import _is_lock_expired
    
    lock_data = {"created_at": "not-a-valid-date"}
    assert not _is_lock_expired(lock_data)
