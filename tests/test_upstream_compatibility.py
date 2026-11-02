"""Behavioral regressions also runnable against the unmodified upstream tree."""

from datetime import datetime, timedelta, timezone
import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

from colab_cli import common, console as transport
from colab_cli.client import ListedAssignment
from colab_cli.commands import execution
from colab_cli.common import State
from colab_cli.state import SessionState, StateStore


def _assignment():
    return ListedAssignment.model_validate({
        "endpoint": "ep1", "variant": 0, "machineShape": 0, "accelerator": "NONE",
        "runtimeProxyInfo": {
            "token": "new-token", "tokenExpiresInSeconds": 3600,
            "url": "https://new-runtime.example.test",
        },
    })


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state = State()
    state._store = StateStore(str(tmp_path / "sessions.json"))
    state._client = MagicMock()
    state._history = MagicMock()
    state.store.add(SessionState(
        name="s1", endpoint="ep1", token="old-token",
        url="https://old-runtime.example.test",
        token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    ))
    monkeypatch.setattr(common, "state", state)
    return state


def test_upstream_proxy_auth_failure_refreshes_before_discarding_session(isolated_state):
    class ProxyAuthError(Exception):
        status_code = 401

    isolated_state.client.list_assignments.return_value = [_assignment()]
    tokens = []

    def connect(session, **kwargs):
        tokens.append(session.token)
        if len(tokens) == 1:
            raise ProxyAuthError("Handshake status 401 Unauthorized")

    with patch.object(execution, "connect_console", side_effect=connect):
        execution.console(session="s1")

    assert tokens == ["old-token", "new-token"]
    assert isolated_state.store.get("s1").endpoint == "ep1"


def test_upstream_refresh_preserves_concurrent_same_name_replacement(isolated_state):
    original = isolated_state.store.get("s1")
    original.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    isolated_state.store.add(original)
    replacement = SessionState(
        name="s1", endpoint="ep2", token="replacement", url="https://replacement",
    )

    def replace_during_lookup(**kwargs):
        isolated_state.store.add(replacement)
        return [_assignment()]

    isolated_state.client.list_assignments.side_effect = replace_during_lookup
    assert isolated_state.get_session("s1") == replacement
    assert isolated_state.store.get("s1") == replacement


def test_upstream_terminal_output_acknowledges_flow_control():
    handler = getattr(transport, "_write_terminal_message", None)
    if handler is None:
        handler = transport.on_message
    ws = MagicMock()
    output = MagicMock()
    with patch.object(transport.sys, "stdout", output):
        handler(ws, json.dumps({"data": "output", "ack": True}))
    output.buffer.flush.assert_called_once()
    assert {"ack": True} in [json.loads(c.args[0]) for c in ws.send.call_args_list]


def test_upstream_abnormal_console_close_reconnects(isolated_state):
    calls = []

    def websocket_attempt(**callbacks):
        ws = MagicMock()
        code = 1006 if not calls else 1000
        calls.append(ws)

        def run(**kwargs):
            callbacks["on_open"](ws)
            callbacks["on_close"](ws, code, "closed")

        ws.run_forever.side_effect = run
        return ws

    kwargs = {}
    if "_max_reconnect_attempts" in inspect.signature(transport.connect_console).parameters:
        kwargs = {"retry_delays": (0,), "_max_reconnect_attempts": 1}
    with (
        patch.object(transport.sys, "stdin", MagicMock()),
        patch.object(transport.threading, "Thread"),
        patch.object(transport.websocket, "WebSocketApp", side_effect=websocket_attempt),
        patch.object(transport.termios, "tcgetattr", return_value=["saved"]),
        patch.object(transport.termios, "tcsetattr"),
        patch.object(transport.tty, "setraw"),
        patch.object(transport.signal, "signal"),
        patch.object(transport.signal, "getsignal"),
    ):
        transport.connect_console(isolated_state.store.get("s1"), **kwargs)
    assert len(calls) == 2
