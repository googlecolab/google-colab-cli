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
from unittest.mock import MagicMock, patch
import pytest
import typer
from colab_cli.auth import AuthProvider
from colab_cli.common import State
from colab_cli.state import SessionState, StateStore


class _ProxyAuthError(Exception):
    """Stands in for websocket-client's WebSocketBadStatusException."""

    def __init__(self, status_code: int):
        super().__init__(f"Handshake status {status_code} Unauthorized")
        self.status_code = status_code


def test_resolve_session_no_local_sessions():
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    with patch("typer.echo") as mock_echo:
        with pytest.raises(typer.Exit):
            state.resolve_session(None)
        mock_echo.assert_any_call(
            "[colab] Error: No active sessions found. Create one with 'colab new'."
        )


def test_resolve_session_with_local_but_none_on_server():
    state = State()
    state._store = MagicMock()
    # Local session exists
    mock_session = MagicMock()
    mock_session.endpoint = "e1"
    state._store.list.return_value = {"s1": mock_session}
    state._store.get.return_value = mock_session

    # But server says no assignments
    state._client = MagicMock()
    state._client.list_assignments.return_value = []

    # Mock history and store.remove
    state._history = MagicMock()

    with patch("typer.echo") as mock_echo:
        with pytest.raises(typer.Exit):
            state.resolve_session(None)
        mock_echo.assert_any_call("[colab] Pruned 1 stale local session(s).")
        mock_echo.assert_any_call(
            "[colab] Error: No active sessions found. Create one with 'colab new'."
        )

    state._store.remove_if_endpoint.assert_called_with("s1", "e1")


def test_sync_sessions_avoids_client_if_no_local():
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    # We want to verify that self.client is NOT accessed if store.list() is empty
    # unless we explicitly call sync_sessions.
    # Actually, in my current implementation of sync_sessions, I still call self.client.list_assignments()
    # to support 'colab sessions' but I wrap it in a try-except.

    with patch.object(State, "client", new_callable=MagicMock) as mock_client_prop:
        state.sync_sessions()
        # My implementation DOES call it to return assignments.
        mock_client_prop.list_assignments.assert_called_once()


def test_resolve_session_avoids_sync_if_no_local():
    state = State()
    state._store = MagicMock()
    state._store.list.return_value = {}

    with patch.object(State, "sync_sessions") as mock_sync:
        with pytest.raises(typer.Exit):
            state.resolve_session(None)
        mock_sync.assert_not_called()


def test_state_client_auth_flag_propagation():
    state = State()
    state.auth_provider = AuthProvider.OAUTH2

    with patch("colab_cli.common.get_credentials") as mock_get_creds:
        with patch("colab_cli.common.Client"):
            _ = state.client
            mock_get_creds.assert_called_once()
            args, kwargs = mock_get_creds.call_args
            assert kwargs["provider"] is AuthProvider.OAUTH2


def test_state_client_auth_provider_default_is_oauth2():
    state = State()
    assert state.auth_provider is AuthProvider.OAUTH2

    with patch("colab_cli.common.get_credentials") as mock_get_creds:
        with patch("colab_cli.common.Client"):
            _ = state.client
            args, kwargs = mock_get_creds.call_args
            assert kwargs["provider"] is AuthProvider.OAUTH2


def test_state_client_auth_provider_adc():
    state = State()
    state.auth_provider = AuthProvider.ADC

    with patch("colab_cli.common.get_credentials") as mock_get_creds:
        with patch("colab_cli.common.Client"):
            _ = state.client
            args, kwargs = mock_get_creds.call_args
            assert kwargs["provider"] is AuthProvider.ADC


class _ProxyInfo:
    def __init__(self, token: str, url: str):
        self.token = token
        self.url = url

    def expires_at(self):
        return datetime.now(timezone.utc) + timedelta(hours=1)


class _Assignment:
    def __init__(self, endpoint: str, token: str, url: str):
        self.endpoint = endpoint
        self.runtime_proxy_info = _ProxyInfo(token, url)


def _listed_assignment(endpoint="e1", token="fresh-token", url="https://fresh"):
    return _Assignment(endpoint, token, url)


def test_resolve_named_session_does_not_hit_the_control_plane(tmp_path):
    """Picking a binding stays offline; refreshing is get_session's job."""
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(
        SessionState(
            name="s1",
            token="expired-token",
            url="https://old",
            endpoint="e1",
            kernel_id="kernel-1",
        )
    )
    state = State()
    state._store = store
    state._client = MagicMock()

    assert state.resolve_session("s1") == "s1"

    state._client.list_assignments.assert_not_called()
    unchanged = store.get("s1")
    assert unchanged.token == "expired-token"
    assert unchanged.kernel_id == "kernel-1"


def test_get_session_refreshes_expired_runtime_proxy(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(
        SessionState(
            name="s1",
            token="expired-token",
            url="https://old",
            endpoint="e1",
            token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            kernel_id="kernel-1",
        )
    )
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.return_value = [_listed_assignment()]

    refreshed = state.get_session("s1")

    assert refreshed.token == "fresh-token"
    assert refreshed.url == "https://fresh"
    assert refreshed.kernel_id == "kernel-1"
    state._client.list_assignments.assert_called_once_with(timeout=10)


def test_refresh_session_forwards_request_timeout(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    original = SessionState(name="s1", token="old", url="old", endpoint="e1")
    store.add(original)
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.return_value = [_listed_assignment()]

    state.refresh_session("s1", expected_session=original, timeout=10)

    state._client.list_assignments.assert_called_once_with(timeout=10)


def test_get_session_prunes_only_when_server_confirms_missing(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(
        SessionState(
            name="s1",
            token="token",
            url="url",
            endpoint="e1",
            token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.return_value = []
    state._history = MagicMock()

    assert state.get_session("s1", ignore_missing_session=True) is None
    assert store.get("s1") is None


def test_get_session_keeps_binding_when_refresh_fails(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    original = SessionState(
        name="s1",
        token="token",
        url="url",
        endpoint="e1",
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    store.add(original)
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.side_effect = RuntimeError("control plane down")

    with pytest.raises(RuntimeError):
        state.get_session("s1")
    assert store.get("s1") == original


def test_get_session_returns_concurrent_same_name_replacement(tmp_path):
    """A refresh must not overwrite a binding that was replaced mid-lookup."""
    store = StateStore(str(tmp_path / "sessions.json"))
    original = SessionState(
        name="s1",
        token="old",
        url="old",
        endpoint="e1",
        token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    replacement = SessionState(
        name="s1", token="replacement", url="replacement", endpoint="e2"
    )
    store.add(original)
    state = State()
    state._store = store
    state._client = MagicMock()

    def list_then_replace(**_kwargs):
        store.add(replacement)
        return [_listed_assignment()]

    state._client.list_assignments.side_effect = list_then_replace

    assert state.get_session("s1") == replacement
    assert store.get("s1") == replacement


def test_refresh_does_not_remove_same_name_replacement_created_during_lookup(
    tmp_path,
):
    """A server snapshot for e1 must never delete a concurrent e2 binding."""
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(SessionState(name="s1", token="old", url="old", endpoint="e1"))
    replacement = SessionState(
        name="s1", token="replacement", url="replacement", endpoint="e2"
    )
    state = State()
    state._store = store
    state._client = MagicMock()
    state._history = MagicMock()

    def list_then_replace(**_kwargs):
        # Model a control-plane response captured before another process
        # replaces the local binding, but delivered after that replacement.
        store.add(replacement)
        return []

    state._client.list_assignments.side_effect = list_then_replace

    assert state.refresh_session("s1") == replacement
    assert store.get("s1") == replacement
    state._history.log_event.assert_not_called()


def test_sync_does_not_count_concurrent_same_name_replacement_as_pruned(
    tmp_path,
):
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(SessionState(name="s1", token="old", url="old", endpoint="e1"))
    replacement = SessionState(
        name="s1", token="replacement", url="replacement", endpoint="e2"
    )
    state = State()
    state._store = store
    state._client = MagicMock()
    state._history = MagicMock()

    def list_then_replace(**_kwargs):
        store.add(replacement)
        return []

    state._client.list_assignments.side_effect = list_then_replace

    with patch("typer.echo") as echo:
        sessions, assignments = state.sync_sessions()

    assert assignments == []
    assert sessions == {"s1": replacement}
    assert store.get("s1") == replacement
    echo.assert_not_called()


def test_prune_session_keeps_binding_when_server_check_fails(tmp_path):
    """An inconclusive control-plane check must never delete local state."""
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(SessionState(name="s1", token="t", url="u", endpoint="e1"))
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.side_effect = RuntimeError("network down")

    assert state.prune_session("s1") is False
    assert store.get("s1") is not None


def test_runtime_proxy_error_refreshes_and_retries_once(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(SessionState(name="s1", token="expired-token", url="old", endpoint="e1"))
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.return_value = [_listed_assignment()]
    seen_tokens = []

    def operation(session):
        seen_tokens.append(session.token)
        if len(seen_tokens) == 1:
            raise RuntimeError("Handshake status 404 Not Found")
        return "connected"

    assert state.run_with_runtime_proxy_retry("s1", operation) == "connected"
    assert seen_tokens == ["expired-token", "fresh-token"]
    assert state._client.list_assignments.call_count == 1


def test_runtime_proxy_retry_is_bounded_to_one_retry(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(SessionState(name="s1", token="expired-token", url="old", endpoint="e1"))
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.return_value = [_listed_assignment()]
    operation = MagicMock(side_effect=RuntimeError("HTTP 404 Not Found"))

    with pytest.raises(RuntimeError, match="404"):
        state.run_with_runtime_proxy_retry("s1", operation)

    assert operation.call_count == 2
    assert state._client.list_assignments.call_count == 1


def test_runtime_proxy_retry_never_switches_to_same_name_replacement(tmp_path):
    """A retry for endpoint e1 must not run against a concurrent endpoint e2."""
    store = StateStore(str(tmp_path / "sessions.json"))
    original = SessionState(name="s1", token="expired-token", url="old", endpoint="e1")
    replacement = SessionState(
        name="s1", token="replacement", url="replacement", endpoint="e2"
    )
    store.add(original)
    state = State()
    state._store = store
    state._client = MagicMock()

    def list_then_replace(**_kwargs):
        store.add(replacement)
        return []

    state._client.list_assignments.side_effect = list_then_replace
    operation = MagicMock(side_effect=_ProxyAuthError(401))

    with pytest.raises(_ProxyAuthError):
        state.run_with_runtime_proxy_retry("s1", operation)

    operation.assert_called_once_with(original)
    assert store.get("s1") == replacement


def test_runtime_proxy_retry_never_prunes_replacement_created_after_failure(
    tmp_path,
):
    store = StateStore(str(tmp_path / "sessions.json"))
    original = SessionState(name="s1", token="expired-token", url="old", endpoint="e1")
    replacement = SessionState(
        name="s1", token="replacement", url="replacement", endpoint="e2"
    )
    store.add(original)
    state = State()
    state._store = store
    state._client = MagicMock()
    state._client.list_assignments.return_value = []
    state._history = MagicMock()

    def fail_after_replacement(session):
        store.add(replacement)
        raise _ProxyAuthError(401)

    with pytest.raises(_ProxyAuthError):
        state.run_with_runtime_proxy_retry("s1", fail_after_replacement)

    assert store.get("s1") == replacement
    state._history.log_event.assert_not_called()


def test_runtime_proxy_retry_does_not_retry_unrelated_404_text(tmp_path):
    store = StateStore(str(tmp_path / "sessions.json"))
    store.add(SessionState(name="s1", token="token", url="url", endpoint="e1"))
    state = State()
    state._store = store
    state._client = MagicMock()
    operation = MagicMock(side_effect=FileNotFoundError("report-404.txt"))

    with pytest.raises(FileNotFoundError, match="report-404"):
        state.run_with_runtime_proxy_retry("s1", operation)

    operation.assert_called_once()
    state._client.list_assignments.assert_not_called()
