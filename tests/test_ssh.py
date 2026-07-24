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

"""Tests for `colab ssh`.

Covers WebSocket URL construction, pubkey resolution (--identity vs ~/.ssh
scan, including its failure paths), per-status error-message mapping, the
connect-failure path, the proxy-mode byte bridge, shell quoting, session
resolution, --rm teardown error handling, and end-to-end dispatch (interactive
vs --proxy-mode).
"""

import io
import subprocess
from unittest.mock import MagicMock

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module
import pytest
import typer
from typer.testing import CliRunner
import websocket

runner = CliRunner()


def _make_session(
    name: str = "s1",
    url: str = "https://abc-foo.colab.googleusercontent.com",
    token: str = "FAKE_TOKEN",
    endpoint: str = "abc123def",
):
    s = MagicMock()
    s.name = name
    s.url = url
    s.token = token
    s.endpoint = endpoint
    return s


# --- WS URL construction -----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("https://abc.colab.googleusercontent.com", "wss"),
        ("http://localhost:8080", "ws"),
    ],
    ids=["https->wss", "http->ws"],
)
def test_build_ws_url_scheme(url, scheme):
    s = _make_session(url=url)
    out = ssh_module._build_ws_url(s)
    netloc = url.split("://", 1)[1]
    assert out.startswith(f"{scheme}://{netloc}/colab/ssh")
    assert "colab-runtime-proxy-token=FAKE_TOKEN" in out


# --- Pubkey resolution -------------------------------------------------------


def test_resolve_pubkey_with_identity_calls_ssh_keygen(mocker, tmp_path):
    key = tmp_path / "id_test"
    key.write_text("(fake private key)")
    fake_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDfake user@host"
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=MagicMock(stdout=fake_pub + "\n", returncode=0),
    )
    out = ssh_module._resolve_pubkey(str(key))
    assert out == fake_pub
    args, _ = mock_run.call_args
    assert args[0][:3] == ["ssh-keygen", "-y", "-f"]
    assert args[0][3] == str(key)


def test_resolve_pubkey_missing_identity_exits(tmp_path):
    missing = tmp_path / "no-such-key"
    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._resolve_pubkey(str(missing))
    assert exc_info.value.exit_code == 2


@pytest.mark.parametrize(
    ("run_side_effect", "run_return"),
    [
        (subprocess.CalledProcessError(1, ["ssh-keygen"]), None),
        (FileNotFoundError("ssh-keygen not installed"), None),
        (None, MagicMock(stdout="   \n", returncode=0)),
    ],
    ids=["ssh-keygen-error", "ssh-keygen-missing", "empty-output"],
)
def test_resolve_pubkey_identity_derivation_failures_exit_2(
    mocker, tmp_path, run_side_effect, run_return
):
    """--identity given but key derivation fails -> clean exit 2 (no traceback).

    The empty-output case is a regression guard: it used to raise an uncaught
    RuntimeError instead of a `typer.Exit`.
    """
    key = tmp_path / "id_test"
    key.write_text("(fake private key)")
    if run_side_effect is not None:
        mocker.patch("subprocess.run", side_effect=run_side_effect)
    else:
        mocker.patch("subprocess.run", return_value=run_return)
    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._resolve_pubkey(str(key))
    assert exc_info.value.exit_code == 2


@pytest.mark.parametrize(
    ("present", "expect_found"),
    [
        ("id_ed25519.pub", True),
        ("id_ecdsa.pub", True),
        ("id_rsa.pub", False),  # RSA is server-rejected -> not auto-selected
        (None, False),  # no keys at all
    ],
    ids=["ed25519", "ecdsa", "rsa-not-selected", "no-keys"],
)
def test_resolve_pubkey_default_scan_key_order(
    monkeypatch, tmp_path, present, expect_found
):
    fake_home = tmp_path / "home"
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    content = ""
    if present:
        content = f"ssh-key-content-for-{present}\n"
        (ssh_dir / present).write_text(content)
    monkeypatch.setattr(
        "os.path.expanduser", lambda p: p.replace("~", str(fake_home))
    )
    if expect_found:
        assert ssh_module._resolve_pubkey(None) == content.strip()
    else:
        with pytest.raises(typer.Exit) as exc_info:
            ssh_module._resolve_pubkey(None)
        assert exc_info.value.exit_code == 2


# --- Per-failure-mode error mapping -----------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "must_contain"),
    [
        (400, b"missing pubkey", "missing pubkey header"),
        (400, b"unsupported key type", "unsupported key type"),
        (400, b"invalid pubkey: bad base64", "Re-check your key"),
        (401, b"", "token is invalid"),
        (403, b"", "Forbidden"),
        (404, b"", "Endpoint not found"),
        (429, b'{"error":"already-active-session"}', "Already-active SSH"),
        (502, b"sshd unreachable", "Bad gateway"),
        (503, b"", "WebSocket upgrade rejected (HTTP 503)"),
        (None, b"", "WebSocket upgrade failed without an HTTP status"),
    ],
)
def test_explain_handshake_failure_mapping(status, body, must_contain):
    out = ssh_module._explain_handshake_failure(status, body)
    assert must_contain in out


def test_explain_handshake_failure_decodes_str_body():
    """A str resp_body is tolerated by the caller's normalization."""
    # _connect_websocket encodes str bodies before calling this; assert the
    # decode path here handles bytes with invalid utf-8 too.
    out = ssh_module._explain_handshake_failure(400, b"\xff\xfe bad")
    assert "HTTP 400" in out


# --- connect failure (non-HTTP-status network errors) ------------------------


@pytest.mark.parametrize(
    "exc",
    [
        websocket.WebSocketAddressException("bad address"),
        websocket.WebSocketTimeoutException("timed out"),
        ConnectionRefusedError("connection refused"),
        OSError("network is down"),
    ],
    ids=["address", "timeout", "refused", "oserror"],
)
def test_connect_websocket_network_failure_exits_1(mocker, capsys, exc):
    mocker.patch.object(websocket.WebSocket, "connect", side_effect=exc)
    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._connect_websocket("wss://host/colab/ssh?x=1", "pk")
    assert exc_info.value.exit_code == 1
    assert "WebSocket connection failed" in capsys.readouterr().err


# --- proxy-mode byte bridge (ws <-> stdout) ---------------------------------


def test_bridge_proxy_mode_pumps_ws_to_stdout(mocker):
    """Binary + text frames reach stdout; a non-data opcode is ignored; CLOSE
    ends the loop; the socket is closed and 0 is returned."""
    # The stdin->ws pump reads a real fd in a thread; stub the thread out so the
    # test deterministically exercises only the ws->stdout direction.
    mocker.patch("threading.Thread")
    mocker.patch("sys.stdin")
    fake_stdout = MagicMock()
    fake_stdout.buffer = io.BytesIO()
    mocker.patch("sys.stdout", fake_stdout)

    abnf = websocket.ABNF
    ws = MagicMock()
    ws.recv_data.side_effect = [
        (abnf.OPCODE_BINARY, b"hello "),
        (abnf.OPCODE_TEXT, "world"),
        (abnf.OPCODE_PING, b""),  # ignored (not BINARY/TEXT/CLOSE)
        (abnf.OPCODE_CLOSE, b""),
    ]

    rc = ssh_module._bridge_proxy_mode(ws)
    assert rc == 0
    assert fake_stdout.buffer.getvalue() == b"hello world"
    ws.close.assert_called()


# --- shell quoting ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [
        ("simple", "simple"),
        ("/abs/path/file", "/abs/path/file"),
        ("a@b.c:d=e,f", "a@b.c:d=e,f"),
        ("with space", "'with space'"),
        ("a'b", "'a'\\''b'"),
        ("", "''"),
    ],
    ids=["word", "path", "safe-punct", "space", "single-quote", "empty"],
)
def test_shquote(raw, quoted):
    assert ssh_module._shquote(raw) == quoted


# --- session resolution ------------------------------------------------------


@pytest.mark.parametrize("found", [True, False], ids=["existing", "missing"])
def test_resolve_session(mock_common_state, found):
    sess = _make_session(name="x")
    mock_common_state.resolve_session.return_value = "x"
    mock_common_state.store.get.return_value = sess if found else None
    if found:
        assert ssh_module._resolve_session("x") is sess
        mock_common_state.store.get.assert_called_with("x")
    else:
        with pytest.raises(typer.Exit) as exc_info:
            ssh_module._resolve_session("x")
        assert exc_info.value.exit_code == 2


# --- --rm teardown error handling -------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expect_raises"),
    [
        (RuntimeError("boom"), False),
        (typer.Exit(3), True),
    ],
    ids=["generic-swallowed", "typer-exit-reraised"],
)
def test_stop_session_error_handling(mocker, capsys, exc, expect_raises):
    """A failed `colab stop` during --rm must not crash the shell, except that
    a `typer.Exit` (a deliberate exit) is allowed to propagate."""
    mocker.patch("colab_cli.commands.session.stop", side_effect=exc)
    if expect_raises:
        with pytest.raises(typer.Exit):
            ssh_module._stop_session("s1")
    else:
        ssh_module._stop_session("s1")  # must not raise
        assert "failed to stop 's1'" in capsys.readouterr().err


# --- end-to-end CLI dispatch ------------------------------------------------


def test_ssh_proxy_mode_calls_websocket(mock_common_state, mocker):
    """--proxy-mode calls _connect_websocket + _bridge_proxy_mode (no ssh)."""
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess

    fake_pub = "ssh-ed25519 AAAAfakefakefake user@host"
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value=fake_pub)

    fake_ws = MagicMock()
    connect = mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=fake_ws
    )
    bridge = mocker.patch.object(
        ssh_module, "_bridge_proxy_mode", return_value=0
    )
    ssh_subprocess = mocker.patch.object(ssh_module, "_run_interactive_ssh")

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert result.exit_code == 0
    connect.assert_called_once()
    args, _ = connect.call_args
    assert args[0].startswith(
        "wss://abc-foo.colab.googleusercontent.com/colab/ssh"
    )
    assert args[1] == fake_pub
    bridge.assert_called_once_with(fake_ws)
    ssh_subprocess.assert_not_called()


def test_ssh_interactive_mode_calls_ssh_subprocess(mock_common_state, mocker):
    """Bare `colab ssh -s S` spawns ssh subprocess; does NOT bridge directly."""
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess

    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAAfake u@h"
    )
    interactive = mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )
    bridge = mocker.patch.object(ssh_module, "_bridge_proxy_mode")

    result = runner.invoke(app, ["ssh", "-s", "s1"])
    assert result.exit_code == 0
    interactive.assert_called_once_with(sess, None)
    bridge.assert_not_called()


def test_ssh_pubkey_passes_through_verbatim(mock_common_state, mocker):
    """The bytes from _resolve_pubkey reach _connect_websocket unchanged.

    Adversarial: confirms there is no intermediate substitution, prefix,
    suffix, or constant - the pubkey arg seen by _connect_websocket is exactly
    the bytes _resolve_pubkey returned.
    """
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess

    payload = "ssh-ed25519 AAAAUNIQUEMARKER1234567890 user@host"
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value=payload)

    captured = {}

    def fake_connect(url, pubkey):
        captured["pubkey"] = pubkey
        captured["url"] = url
        return MagicMock()

    mocker.patch.object(
        ssh_module, "_connect_websocket", side_effect=fake_connect
    )
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert captured["pubkey"] == payload  # verbatim


@pytest.mark.parametrize(
    "resp_body",
    [b"unsupported key type", "unsupported key type"],
    ids=["bytes-body", "str-body"],
)
def test_ssh_handshake_400_emits_actionable_message(
    mock_common_state, mocker, resp_body
):
    """A 400 'unsupported key type' surfaces the keygen remediation hint.

    Parametrized over a bytes vs str resp_body so the str-normalization path in
    _connect_websocket is exercised too.
    """
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-rsa AAAAfake u@h"
    )

    err = websocket.WebSocketBadStatusException(
        "Handshake status 400 Bad Request", 400
    )
    err.status_code = 400
    err.resp_body = resp_body
    mocker.patch.object(websocket.WebSocket, "connect", side_effect=err)
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert result.exit_code == 1
    assert "unsupported key type" in result.stderr
    assert "ssh-keygen -t ed25519" in result.stderr
