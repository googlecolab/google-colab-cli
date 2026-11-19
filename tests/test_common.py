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

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import typer

from colab_cli.client import ListedAssignment
from colab_cli.common import State
from colab_cli.state import SessionState, StateStore


def _assignment(endpoint, token, expires_in=3600, url="https://new-url"):
    return ListedAssignment.model_validate(
        {
            "accelerator": "NONE",
            "endpoint": endpoint,
            "variant": 0,
            "machineShape": 0,
            "runtimeProxyInfo": {
                "token": token,
                "tokenExpiresInSeconds": expires_in,
                "url": url,
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


def test_get_session_public_api_refreshes_via_get_runtime(real_state):
    from colab_cli.client import Runtime

    real_state.client.use_public_api = True
    real_state.store.add(
        SessionState(
            name="s1",
            token="old",
            url="https://old",
            endpoint="",
            runtime_name="runtimes/r-1",
            token_expires_at=None,
        )
    )
    future_iso = (
        (datetime.now(timezone.utc) + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    real_state.client.get_runtime.return_value = Runtime.model_validate(
        {
            "name": "runtimes/r-1",
            "connectionInfo": {
                "expireTime": future_iso,
                "url": "https://refreshed",
                "token": "refreshed-tok",
            },
            "runtimeSpec": {
                "variant": "VARIANT_CPU",
                "accelerator": "NONE",
                "shape": "SHAPE_STANDARD",
            },
        }
    )

    s = real_state.get_session("s1")

    assert s.token == "refreshed-tok"
    assert s.url == "https://refreshed"
    real_state.client.get_runtime.assert_called_once_with("runtimes/r-1")
    real_state.client.list_assignments.assert_not_called()


def test_get_session_public_api_prunes_on_404(real_state):
    from colab_cli.client import ColabRequestError

    real_state.client.use_public_api = True
    real_state.store.add(
        SessionState(
            name="s1",
            token="old",
            url="https://old",
            endpoint="",
            runtime_name="runtimes/r-1",
            token_expires_at=None,
        )
    )
    real_state.client.get_runtime.side_effect = ColabRequestError(
        "not found", request=None, response=MagicMock(status_code=404)
    )

    assert real_state.get_session("s1", ignore_missing_session=True) is None
    assert real_state.store.get("s1") is None


def test_sync_sessions_matches_by_runtime_name_when_endpoint_empty(real_state):
    real_state.store.add(
        SessionState(
            name="s1", token="old1", url="u", endpoint="", runtime_name="runtimes/r-1"
        )
    )
    real_state.store.add(
        SessionState(
            name="s2", token="old2", url="u", endpoint="", runtime_name="runtimes/r-2"
        )
    )
    a1 = _assignment("", "tok-1")
    a1.name = "runtimes/r-1"
    a2 = _assignment("", "tok-2")
    a2.name = "runtimes/r-2"
    real_state.client.list_assignments.return_value = [a1, a2]

    sessions, _ = real_state.sync_sessions()

    assert sessions["s1"].token == "tok-1"
    assert sessions["s2"].token == "tok-2"


def test_sync_sessions_matches_by_url_and_backfills_runtime_name(real_state):
    real_state.store.add(
        SessionState(
            name="s1",
            token="old",
            url="https://vm-1.colab.dev/",
            endpoint="ep-1",
            runtime_name=None,
        )
    )
    a1 = _assignment("", "pub-tok", url="https://vm-1.colab.dev")
    a1.name = "runtimes/r-1"
    real_state.client.list_assignments.return_value = [a1]

    sessions, _ = real_state.sync_sessions()

    assert "s1" in sessions
    assert sessions["s1"].token == "pub-tok"
    assert sessions["s1"].runtime_name == "runtimes/r-1"
    assert real_state.store.get("s1").runtime_name == "runtimes/r-1"


def test_sync_sessions_matches_by_url_and_backfills_endpoint(real_state):
    real_state.store.add(
        SessionState(
            name="s1",
            token="old",
            url="https://vm-1.colab.dev",
            endpoint="",
            runtime_name="runtimes/r-1",
        )
    )
    a1 = _assignment("ep-1", "tun-tok", url="https://vm-1.colab.dev/")
    real_state.client.list_assignments.return_value = [a1]

    sessions, _ = real_state.sync_sessions()

    assert "s1" in sessions
    assert sessions["s1"].token == "tun-tok"
    assert sessions["s1"].endpoint == "ep-1"
    assert real_state.store.get("s1").endpoint == "ep-1"


def test_sync_sessions_by_url_rejects_conflicting_endpoint(real_state):
    real_state.store.add(
        SessionState(
            name="s1",
            token="old",
            url="https://shared.colab.dev",
            endpoint="ep-1",
            runtime_name=None,
        )
    )
    a2 = _assignment("ep-2", "other-tok", url="https://shared.colab.dev")
    real_state.client.list_assignments.return_value = [a2]

    sessions, _ = real_state.sync_sessions()

    assert "s1" not in sessions
    assert real_state.store.get("s1") is None


def test_get_session_fresh_token_does_not_touch_credentials(tmp_path, mocker):
    from colab_cli.state import Settings, SettingsStore

    get_creds = mocker.patch("colab_cli.common.get_credentials")
    st = State()
    st._store = StateStore(str(tmp_path / "sessions.json"))
    st._settings_store = SettingsStore(str(tmp_path / "settings.json"))
    st._settings_store.save(Settings(use_public_api=False))
    fresh_expiry = datetime.now(timezone.utc) + timedelta(minutes=45)

    st.store.add(
        SessionState(
            name="pub",
            token="t",
            url="https://vm-1.colab.dev",
            endpoint="",
            runtime_name="runtimes/r-1",
            token_expires_at=fresh_expiry,
        )
    )
    st.store.add(
        SessionState(
            name="tun",
            token="t",
            url="https://vm-2.colab.dev",
            endpoint="ep-2",
            runtime_name=None,
            token_expires_at=fresh_expiry,
        )
    )

    st.use_public_api = False
    assert st.get_session("pub").runtime_name == "runtimes/r-1"
    assert st.get_session("tun", require_backend_id=True).endpoint == "ep-2"

    st.use_public_api = True
    assert st.get_session("tun").endpoint == "ep-2"
    assert st.get_session("pub", require_backend_id=True).runtime_name == "runtimes/r-1"

    get_creds.assert_not_called()
    assert st._client is None


def test_get_session_cross_api_backfills_via_by_url_even_when_token_fresh(real_state):
    fresh_expiry = datetime.now(timezone.utc) + timedelta(minutes=45)
    real_state.client.use_public_api = True
    real_state.store.add(
        SessionState(
            name="tun-sess",
            token="old",
            url="https://vm-1.colab.dev/",
            endpoint="ep-1",
            runtime_name=None,
            token_expires_at=fresh_expiry,
        )
    )
    a_pub = _assignment("", "pub-tok", url="https://vm-1.colab.dev")
    a_pub.name = "runtimes/r-1"
    real_state.client.list_assignments.return_value = [a_pub]

    assert real_state.get_session("tun-sess").runtime_name is None
    real_state.client.list_assignments.assert_not_called()

    s = real_state.get_session("tun-sess", require_backend_id=True)

    assert s.runtime_name == "runtimes/r-1"
    assert real_state.store.get("tun-sess").runtime_name == "runtimes/r-1"

    real_state.client.use_public_api = False
    real_state.store.add(
        SessionState(
            name="pub-sess",
            token="old",
            url="https://vm-2.colab.dev",
            endpoint="",
            runtime_name="runtimes/r-2",
            token_expires_at=fresh_expiry,
        )
    )
    a_tun = _assignment("ep-2", "tun-tok", url="https://vm-2.colab.dev/")
    real_state.client.list_assignments.return_value = [a_tun]

    s2 = real_state.get_session("pub-sess", require_backend_id=True)

    assert s2.endpoint == "ep-2"
    assert real_state.store.get("pub-sess").endpoint == "ep-2"


def test_match_session_matches_and_rejects_conflicts():
    from colab_cli.common import index_sessions, match_session

    s_pub = SessionState(
        name="pub",
        token="t",
        url="https://vm-1.colab.dev/",
        endpoint="",
        runtime_name="runtimes/r-1",
    )
    s_tun = SessionState(
        name="tun",
        token="t",
        url="https://vm-2.colab.dev",
        endpoint="ep-2",
        runtime_name=None,
    )
    idx = index_sessions([s_pub, s_tun])

    a_by_rt = _assignment("", "t", url="https://other.colab.dev")
    a_by_rt.name = "runtimes/r-1"
    assert match_session(a_by_rt, idx) == s_pub

    a_by_ep = _assignment("ep-2", "t", url="https://other.colab.dev")
    assert match_session(a_by_ep, idx) == s_tun

    a_by_url = _assignment("ep-backfill", "t", url="https://vm-1.colab.dev")
    assert match_session(a_by_url, idx) == s_pub

    a_conflict_rt = _assignment("", "t", url="https://vm-1.colab.dev")
    a_conflict_rt.name = "runtimes/r-other"
    assert match_session(a_conflict_rt, idx) is None

    a_conflict_ep = _assignment("ep-other", "t", url="https://vm-2.colab.dev")
    assert match_session(a_conflict_ep, idx) is None

    a_unmatched = _assignment("", "t", url="")
    assert match_session(a_unmatched, idx) is None
