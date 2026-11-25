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

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import typer

from colab_cli.client import ListedAssignment
from colab_cli.common import State, setup_logging
from colab_cli.state import SessionState, SettingsStore, StateStore


def _assignment(endpoint, token, expires_in=3600):
    return ListedAssignment.model_validate(
        {
            "accelerator": "NONE",
            "endpoint": endpoint,
            "variant": 0,
            "machineShape": 0,
            "runtimeProxyInfo": {
                "token": token,
                "tokenExpiresInSeconds": expires_in,
                "url": "https://new-url",
            },
        }
    )


def _session(expires_at):
    return SessionState(
        name="s1",
        token="old-token",
        url="https://old-url",
        endpoint="ep1",
        token_expires_at=expires_at,
    )


@pytest.fixture
def real_state(tmp_path):
    st = State()
    st._store = StateStore(str(tmp_path / "sessions.json"))
    st._client = MagicMock()
    st._history = MagicMock()
    return st


def test_get_session_fresh_token_skips_refresh(real_state):
    real_state.store.add(_session(datetime.now(timezone.utc) + timedelta(minutes=30)))

    s = real_state.get_session("s1")

    assert s.token == "old-token"
    real_state.client.list_assignments.assert_not_called()


@pytest.mark.parametrize(
    "expires_at",
    [
        None,
        datetime.now(timezone.utc) - timedelta(minutes=1),
        datetime.now(timezone.utc) + timedelta(minutes=2),
    ],
    ids=["missing", "expired", "within_margin"],
)
def test_get_session_refreshes_stale_token(real_state, expires_at):
    real_state.store.add(_session(expires_at))
    real_state.client.list_assignments.return_value = [
        _assignment("other", "wrong-token"),
        _assignment("ep1", "new-token"),
    ]

    s = real_state.get_session("s1")

    assert s.token == "new-token"
    assert s.url == "https://new-url"
    assert s.token_expires_at > datetime.now(timezone.utc) + timedelta(minutes=55)
    persisted = real_state.store.get("s1")
    assert persisted.token == "new-token"
    assert persisted.token_expires_at == s.token_expires_at


def test_get_session_prunes_when_assignment_gone(real_state):
    real_state.store.add(_session(None))
    real_state.client.list_assignments.return_value = []

    assert real_state.get_session("s1", ignore_missing_session=True) is None
    assert real_state.store.get("s1") is None


def test_get_session_unknown_name_exits(real_state, capsys):
    with pytest.raises(typer.Exit) as exc:
        real_state.get_session("nope")

    assert exc.value.exit_code == 1
    assert "Session 'nope' not found." in capsys.readouterr().out
    real_state.client.list_assignments.assert_not_called()


def test_get_session_unknown_name_ignored(real_state):
    assert real_state.get_session("nope", ignore_missing_session=True) is None


def test_sync_sessions_updates_tokens(real_state):
    real_state.store.add(_session(None))
    real_state.client.list_assignments.return_value = [_assignment("ep1", "new-token")]

    sessions, _ = real_state.sync_sessions()

    assert sessions["s1"].token == "new-token"
    assert real_state.store.get("s1").token == "new-token"
    assert real_state.store.get("s1").token_expires_at is not None


@pytest.mark.parametrize(
    "flag_val,setting_val,expected",
    [
        (None, False, False),
        (None, True, True),
        (True, False, True),
        (False, True, False),
    ],
)
def test_state_client_use_public_api_precedence(
    tmp_path, mocker, flag_val, setting_val, expected
):
    from colab_cli.state import Settings, SettingsStore

    st = State()
    st._settings_store = SettingsStore(str(tmp_path / "settings.json"))
    st._settings_store.save(Settings(use_public_api=setting_val))
    st.use_public_api = flag_val
    mocker.patch("colab_cli.common.get_credentials", return_value=MagicMock())

    assert st.client.use_public_api is expected


def test_get_session_refresh_preserves_attached_flag(real_state):
    attached_session = _session(None)
    attached_session.attached = True
    real_state.store.add(attached_session)
    real_state.client.list_assignments.return_value = [_assignment("ep1", "new-token")]

    refreshed = real_state.get_session("s1")

    assert refreshed is not None and refreshed.attached is True
    assert refreshed.token == "new-token"
    assert real_state.store.get("s1").attached is True


def test_sync_sessions_preserves_attached_flag(real_state):
    attached_session = _session(None)
    attached_session.attached = True
    real_state.store.add(attached_session)
    real_state.client.list_assignments.return_value = [_assignment("ep1", "new-token")]

    sessions, _ = real_state.sync_sessions()

    assert sessions["s1"].attached is True
    assert real_state.store.get("s1").attached is True


def test_state_store_derives_default_path_from_settings_store(tmp_path):
    settings_dir = tmp_path / "cfg"
    local_state = State()
    local_state._settings_store = SettingsStore(str(settings_dir / "settings.json"))

    assert local_state.store.path == str(settings_dir / "sessions.json")


def test_state_store_uses_explicit_config_path(tmp_path):
    override = tmp_path / "override.json"
    local_state = State()
    local_state._settings_store = SettingsStore(str(tmp_path / "cfg" / "settings.json"))
    local_state.config_path = str(override)

    assert local_state.store.path == str(override)


def test_setup_logging_uses_settings_store_directory(tmp_path, monkeypatch):
    settings_dir = tmp_path / "custom-log-dir"
    local_state = State()
    local_state._settings_store = SettingsStore(str(settings_dir / "settings.json"))
    monkeypatch.setattr("colab_cli.common.state", local_state)
    root_logger = logging.getLogger()
    monkeypatch.setattr(root_logger, "handlers", [])
    try:
        setup_logging(False)
        logging.getLogger("colab_cli.test").info("hello-from-settings-dir")
    finally:
        root_logger.handlers[0].close()

    assert "hello-from-settings-dir" in (settings_dir / "colab.log").read_text()
