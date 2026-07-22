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

"""Tests for `colab ssh`: connect to a Colab runtime via SSH-over-WebSocket.

Covers WebSocket URL construction, pubkey resolution (--identity vs ~/.ssh
scan), per-status error-message mapping, shell quoting, session resolution,
and end-to-end dispatch (interactive vs --proxy-mode).
"""

import json
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


def test_build_ws_url_https_uses_wss():
    s = _make_session(url="https://abc.colab.googleusercontent.com")
    out = ssh_module._build_ws_url(s)
    assert out.startswith("wss://abc.colab.googleusercontent.com/colab/ssh")
    assert "colab-runtime-proxy-token=FAKE_TOKEN" in out


def test_build_ws_url_http_uses_ws():
    s = _make_session(url="http://localhost:8080")
    out = ssh_module._build_ws_url(s)
    assert out.startswith("ws://localhost:8080/colab/ssh")
    assert "colab-runtime-proxy-token=FAKE_TOKEN" in out


# --- Pubkey resolution -------------------------------------------------------


def test_resolve_pubkey_with_identity_calls_ssh_keygen(mocker, tmp_path):
    key = tmp_path / "id_test"
    key.write_text("(fake private key)")
    fake_pub = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDfakefakefakefakefakefakefake user@host"
    )
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=MagicMock(stdout=fake_pub + "\n", returncode=0),
    )
    out = ssh_module._resolve_pubkey(str(key))
    assert out == fake_pub
    args, kwargs = mock_run.call_args
    assert args[0][:3] == ["ssh-keygen", "-y", "-f"]
    assert args[0][3] == str(key)


def test_resolve_pubkey_missing_identity_exits(tmp_path):
    missing = tmp_path / "no-such-key"
    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._resolve_pubkey(str(missing))
    assert exc_info.value.exit_code == 2


def test_resolve_pubkey_default_scans_ssh_dir(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    fake_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDanother user@host\n"
    (ssh_dir / "id_ed25519.pub").write_text(fake_pub)

    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(fake_home)))
    out = ssh_module._resolve_pubkey(None)
    assert out == fake_pub.strip()


def test_resolve_pubkey_default_no_keys_exits(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(fake_home)))
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


# --- shell quoting ----------------------------------------------------------


def test_shquote_safe_chars_unquoted():
    assert ssh_module._shquote("simple") == "simple"
    assert ssh_module._shquote("/abs/path/file") == "/abs/path/file"
    assert ssh_module._shquote("a@b.c:d=e,f") == "a@b.c:d=e,f"


def test_shquote_unsafe_chars_quoted():
    assert ssh_module._shquote("with space") == "'with space'"
    assert ssh_module._shquote("a'b") == "'a'\\''b'"
    assert ssh_module._shquote("") == "''"


# --- session resolution ------------------------------------------------------


def test_resolve_session_existing_returns_session(mock_common_state):
    sess = _make_session(name="existing")
    mock_common_state.resolve_session.return_value = "existing"
    mock_common_state.store.get.return_value = sess
    out = ssh_module._resolve_session("existing")
    assert out is sess
    mock_common_state.store.get.assert_called_with("existing")


def test_resolve_session_missing_exits(mock_common_state):
    mock_common_state.resolve_session.return_value = "ghost"
    mock_common_state.store.get.return_value = None
    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._resolve_session("ghost")
    assert exc_info.value.exit_code == 2


# --- end-to-end CLI dispatch ------------------------------------------------


def test_ssh_proxy_mode_calls_websocket(mock_common_state, mocker, tmp_path):
    """`--proxy-mode` calls _connect_websocket + _bridge_proxy_mode (no ssh subprocess)."""
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess

    fake_pub = "ssh-ed25519 AAAAfakefakefake user@host"
    mocker.patch.object(ssh_module, "_resolve_pubkey", return_value=fake_pub)

    fake_ws = MagicMock()
    connect = mocker.patch.object(
        ssh_module, "_connect_websocket", return_value=fake_ws
    )
    bridge = mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)
    ssh_subprocess = mocker.patch.object(ssh_module, "_run_interactive_ssh")

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert result.exit_code == 0
    connect.assert_called_once()
    args, _ = connect.call_args
    assert args[0].startswith("wss://abc-foo.colab.googleusercontent.com/colab/ssh")
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

    mocker.patch.object(ssh_module, "_connect_websocket", side_effect=fake_connect)
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert captured["pubkey"] == payload  # verbatim


def test_ssh_handshake_400_emits_actionable_message(mock_common_state, mocker):
    """A 400 with body 'unsupported key type' surfaces the keygen remediation hint."""
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-rsa AAAAfake u@h"
    )

    err = websocket.WebSocketBadStatusException("Handshake status 400 Bad Request", 400)
    err.status_code = 400
    err.resp_body = b"unsupported key type"
    mocker.patch.object(websocket.WebSocket, "connect", side_effect=err)
    mocker.patch.object(ssh_module, "_bridge_proxy_mode", return_value=0)

    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1"])
    assert result.exit_code == 1
    assert "unsupported key type" in result.stderr
    assert "ssh-keygen -t ed25519" in result.stderr


# Mirrors the shape gcloud's application-default login writes on disk.
_FAKE_ADC = {
    "type": "authorized_user",
    "client_id": "764086051850-fake.apps.googleusercontent.com",
    "client_secret": "FAKE_CLIENT_SECRET",
    "refresh_token": "FAKE_REFRESH_TOKEN",
    "quota_project_id": "my-quota-project",
}


def test_ssh_drive_flag_in_help():
    """`colab ssh --help` advertises the --drive flag (a broken wiring fails)."""
    result = runner.invoke(app, ["ssh", "--help"])
    assert result.exit_code == 0
    assert "--drive" in result.output


def test_ssh_drive_flag_mints_drive_scoped_token(mock_common_state, mocker, tmp_path):
    """--drive shells to gcloud with the drive.file scope, captures the token +
    quota-project, pushes it, then still opens the interactive shell."""
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )

    adc_path = tmp_path / "application_default_credentials.json"
    mocker.patch.object(ssh_module, "_ADC_PATH", str(adc_path))

    def fake_run(cmd, *args, **kwargs):
        # gcloud's browser consent is simulated by writing the ADC file it would
        # write; NEVER invoke real gcloud (would open a browser / hang).
        if cmd[:4] == ["gcloud", "auth", "application-default", "login"]:
            adc_path.write_text(json.dumps(_FAKE_ADC))
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run = mocker.patch("subprocess.run", side_effect=fake_run)
    push = mocker.patch.object(ssh_module, "_push_drive_token")
    interactive = mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )

    result = runner.invoke(app, ["ssh", "-s", "s1", "--drive"])
    assert result.exit_code == 0

    # (a) the Drive-scope gcloud sign-in happened, carrying the drive.file scope.
    gcloud_logins = [
        c
        for c in mock_run.call_args_list
        if c.args[0][:4] == ["gcloud", "auth", "application-default", "login"]
    ]
    assert len(gcloud_logins) == 1
    scopes_arg = next(a for a in gcloud_logins[0].args[0] if a.startswith("--scopes="))
    assert ssh_module._DRIVE_SCOPE in scopes_arg

    # (b) the minted token (frozen-schema fields) was captured from the ADC file.
    push.assert_called_once()
    pushed_session, pushed_identity, token = push.call_args.args
    assert pushed_session is sess
    assert token["refresh_token"] == "FAKE_REFRESH_TOKEN"
    assert token["client_id"] == _FAKE_ADC["client_id"]
    assert token["client_secret"] == "FAKE_CLIENT_SECRET"
    assert token["quota_project_id"] == "my-quota-project"
    assert token["token_uri"] == "https://oauth2.googleapis.com/token"
    assert ssh_module._DRIVE_SCOPE in token["scopes"]

    # (c) the interactive shell still opens after the token is installed.
    interactive.assert_called_once()


def test_ssh_drive_flag_absent_no_gcloud_or_scp(mock_common_state, mocker):
    """Without --drive the path is unchanged: no gcloud, no token push, and the
    interactive shell is still spawned."""
    sess = _make_session()
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.store.get.return_value = sess
    mocker.patch.object(
        ssh_module, "_resolve_pubkey", return_value="ssh-ed25519 AAAA u@h"
    )

    mint = mocker.patch.object(ssh_module, "_mint_drive_token")
    push = mocker.patch.object(ssh_module, "_push_drive_token")
    interactive = mocker.patch.object(
        ssh_module, "_run_interactive_ssh", return_value=0
    )

    result = runner.invoke(app, ["ssh", "-s", "s1"])
    assert result.exit_code == 0
    mint.assert_not_called()
    push.assert_not_called()
    interactive.assert_called_once_with(sess, None)


def test_ssh_drive_flag_uses_frozen_contract_constants():
    """The cross-repo contract literals equal their real values (anti-tautology
    for the scope assertion; F011b will pin these bytes via a golden fixture)."""
    assert ssh_module._DRIVE_SCOPE == "https://www.googleapis.com/auth/drive.file"
    assert ssh_module._DRIVE_TOKEN_REMOTE_PATH == "/root/.config/colab/drive_token.json"
    assert ssh_module._DRIVE_TOKEN_MODE == 0o600


def test_ssh_drive_flag_installs_token_0600_at_frozen_path(mocker):
    """_push_drive_token installs the token at the frozen path, mode 0600, over
    the existing ProxyCommand ssh channel, streaming the JSON via stdin."""
    sess = _make_session()
    token = {
        "refresh_token": "RT",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "CID",
        "client_secret": "CSEC",
        "quota_project_id": "QP",
        "scopes": [ssh_module._DRIVE_SCOPE],
    }
    mock_run = mocker.patch("subprocess.run", return_value=MagicMock(returncode=0))

    ssh_module._push_drive_token(sess, None, token)

    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "ssh"
    assert "root@colab-runtime" in cmd
    remote_cmd = cmd[-1]
    assert "install -D -m 600" in remote_cmd
    assert "/root/.config/colab/drive_token.json" in remote_cmd
    payload = json.loads(mock_run.call_args.kwargs["input"])
    assert payload["refresh_token"] == "RT"
    assert ssh_module._DRIVE_SCOPE in payload["scopes"]


def test_ssh_drive_flag_quota_project_fallback_to_gcloud_config(mocker, tmp_path):
    """When the ADC file omits quota_project_id, _mint_drive_token falls back to
    `gcloud config get-value project`."""
    adc_no_quota = {
        "type": "authorized_user",
        "client_id": "CID",
        "client_secret": "CSEC",
        "refresh_token": "RT",
    }
    adc_path = tmp_path / "adc.json"
    mocker.patch.object(ssh_module, "_ADC_PATH", str(adc_path))

    def fake_run(cmd, *args, **kwargs):
        if cmd[:4] == ["gcloud", "auth", "application-default", "login"]:
            adc_path.write_text(json.dumps(adc_no_quota))
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gcloud", "config", "get-value"]:
            return MagicMock(returncode=0, stdout="fallback-project\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mocker.patch("subprocess.run", side_effect=fake_run)

    token = ssh_module._mint_drive_token()
    assert token["quota_project_id"] == "fallback-project"
