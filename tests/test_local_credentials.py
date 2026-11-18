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

import functools
import json
import logging
import os
import stat
from unittest.mock import MagicMock

import pytest
import requests

from colab_cli.client import Client, Prod
from colab_cli.commands.ssh import _build_ws_url
from colab_cli.common import setup_logging
from colab_cli.history import HistoryLogger
from colab_cli.state import SessionState, Settings, SettingsStore, StateStore

LOGGER_NAMES = (None, "urllib3", "jupyter_kernel_client")
LOG_FILE = ".config/colab-cli/colab.log"
OWNER_ONLY = 0o600
WORLD_READABLE = 0o644
DEFAULT_UMASK = 0o022
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
SESSION = SessionState(name="s", token="t", url="https://h", endpoint="e")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    levels = {name: logging.getLogger(name).level for name in LOGGER_NAMES}
    yield tmp_path
    for handler in logging.getLogger().handlers:
        handler.close()
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def _add_session(home):
    StateStore(f"{home}/sessions.json").add(SESSION)


def _save_settings(home):
    SettingsStore(f"{home}/settings.json").save(Settings())


_PRIVATE_FILES = {
    LOG_FILE: lambda home: setup_logging(False),
    "sessions.json": _add_session,
    "settings.json": _save_settings,
    "s.jsonl": lambda home: HistoryLogger(str(home)).log_event("s", "x", {}),
}


@POSIX_ONLY
@pytest.mark.parametrize(
    "has_existing_file",
    [False, True],
    ids=["fresh", "was-0644"],
)
@pytest.mark.parametrize("relative", list(_PRIVATE_FILES))
def test_files_are_private_to_the_user(home, relative, has_existing_file):
    path = home / relative
    umask = os.umask(DEFAULT_UMASK)
    try:
        if has_existing_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(WORLD_READABLE)
        _PRIVATE_FILES[relative](home)
    finally:
        os.umask(umask)
    assert stat.S_IMODE(path.stat().st_mode) == OWNER_ONLY


@POSIX_ONLY
def test_existing_log_is_kept_and_made_private(home):
    path = home / LOG_FILE
    path.parent.mkdir(parents=True)
    path.write_text("earlier line\n")
    path.chmod(WORLD_READABLE)
    setup_logging(False)
    logging.getLogger("colab_cli").info("later line")
    assert path.read_text().startswith("earlier line\n")
    assert "later line" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == OWNER_ONLY


@POSIX_ONLY
def test_history_files_are_made_private_when_only_read(home):
    path = home / "old.jsonl"
    path.write_text('{"event_type": "x"}\n')
    path.chmod(WORLD_READABLE)
    assert HistoryLogger(str(home)).get_history("old")
    assert stat.S_IMODE(path.stat().st_mode) == OWNER_ONLY


@pytest.mark.parametrize("relative", list(_PRIVATE_FILES))
def test_chmod_failure_is_not_fatal(home, monkeypatch, relative):
    monkeypatch.setattr(os, "chmod", MagicMock(side_effect=PermissionError))
    _PRIVATE_FILES[relative](home)
    assert (home / relative).exists()


SECRET = "sentinel-0123456789abcdef"
QUERY = f"/api/contents/x?authuser=0&colab-runtime-proxy-token={SECRET}"
BODY = json.dumps({"endpoint": "e1", "runtimeProxyInfo": {"token": SECRET}})
ESCAPED_JSON = json.dumps(json.dumps({"token": SECRET, "next": "e1"}))
ESCAPED_VALUE = f'{{"token": "abc\\u003d{SECRET}", "next": "e1"}}'
ESCAPED_INNER = json.dumps(f'{{"code": "4\\/{SECRET}", "next": "e1"}}')
ESCAPED_BACKSLASH = json.dumps({"token": "ab\\" + SECRET, "next": "e1"})
BYTES_VALUE = {b"token": SECRET.encode(), b"kind": b"k"}
AUTH = {"Authorization": f"Bearer {SECRET}", "Accept": "text/html"}
PROXY_HEADERS = {"X-Colab-Runtime-Proxy-Token": SECRET, "X-Colab-Tunnel": "G"}
RAW_PROXY_TOKEN = f"X-Colab-Runtime-Proxy-Token: {SECRET}"
RAW_AUTH = f"Authorization: Bearer {SECRET}"
RAW_BASIC = f"Sending Basic {SECRET}"
BYTES_HEADERS = {b"Authorization": f"Bearer {SECRET}".encode(), b"Accept": b"*"}
KERNEL_WS = f"wss://h/api/kernels/k/channels?session_id=x&token={SECRET}"
SSH_WS = _build_ws_url(SESSION.model_copy(update={"token": SECRET}))
ERROR = requests.ConnectionError(f"Max retries exceeded: {QUERY}")
LOOK_ALIKES = (
    "xcode=1",
    '"code": 200',
    "basic auth",
    "Bearer 1234567",
    "%26xcode%3D1",
    "%26barcode%3D1",
    "%3Fcode_challenge%3Dabc",
    "\\u0026xcode\\u003d1",
    "\\u003dbarcode\\u003d1",
)
LOG = logging.getLogger("colab_cli.test")
KERNEL_CLIENT_LOG = logging.getLogger("jupyter_kernel_client")


@pytest.fixture
def read_log(home):
    setup_logging(False)
    return lambda: (home / LOG_FILE).read_text()


def _issue_request(body="", headers=None):
    response = MagicMock(ok=True, status_code=200, reason="OK", text=body)
    response.headers = {}
    response.request.headers = headers or {}
    session = MagicMock(**{"request.return_value": response})
    url = "https://colab.research.google.com/tun/m/assignments"
    Client(Prod(), session)._issue_request(url)


def _urllib3_get():
    args = ("https", "h", 443, "GET", QUERY, "HTTP/1.1", 200, 2)
    fmt = '%s://%s:%s "%s %s %s" %s %s'
    logging.getLogger("urllib3.connectionpool").debug(fmt, *args)


def _kernel_stdin_reply():
    KERNEL_CLIENT_LOG.debug("input_reply %s", SECRET)
    KERNEL_CLIENT_LOG.info("ready")


_LOGGED_TEXT = {
    "authorization-raw": (RAW_AUTH, "Authorization: Bearer"),
    "basic-credential": (RAW_BASIC, "Sending Basic "),
    "bytes-header": (BYTES_HEADERS, "b'Bearer "),
    "proxy-token-header": (PROXY_HEADERS, "Tunnel"),
    "proxy-token-raw": (RAW_PROXY_TOKEN, "Proxy-Token: "),
    "query-wss-ssh": (SSH_WS, "wss://h/colab/ssh?"),
    "query-wss-kernel": (KERNEL_WS, "session_id=x&"),
    "cookie-header": ({"Cookie": f"SID={SECRET}", "Accept": "*/*"}, "'Accept'"),
    "set-cookie-header": (f"Set-Cookie: sid={SECRET}", "Set-Cookie: "),
    "goog-token-header": (f"X-Goog-Colab-Token: {SECRET}", "Colab-Token: "),
    "auth-tuple": ([("Authorization", f"Token {SECRET}")], "('Authorization'"),
    "bytes-key": ({b"token": SECRET, b"kind": "k"}, "b'kind'"),
    "bytes-value": (BYTES_VALUE, "b'kind': b'k'"),
    "escaped-json-key": (ESCAPED_JSON, '\\"next\\": \\"e1\\"'),
    "escaped-json-inner-escape": (ESCAPED_INNER, '\\"next\\": \\"e1\\"'),
    "percent-escaped-value": (f"code=4%2F{SECRET}&state=ok", "&state=ok"),
    "bearer-text": (f"Retrying with Bearer {SECRET}", "with Bearer "),
    "encoded-amp": (f"next=%2Fcb%3Fstate%3D1%26code%3D{SECRET}", "%26code%3D"),
    "encoded-query": (f"cb%3Fcode_verifier%3D{SECRET}", "%3Fcode_verifier%3D"),
    "encoded-form": (f"a%3D1%26client_secret%3D{SECRET}%26b%3D2", "%26b%3D2"),
    "encoded-lowercase": (f"cb%3fcode%3d{SECRET}", "%3fcode%3d"),
    "encoded-semicolon": (f"a%3D1%3Bcode%3D{SECRET}", "%3Bcode%3D"),
    "encoded-hash": (f"cb%23code%3D{SECRET}", "%23code%3D"),
    "encoded-equals-in-value": (f"next%3Dcode%253D{SECRET}", "%3Dcode%253D"),
    "json-escaped-amp": (f"state=1\\u0026code={SECRET}", "\\u0026code="),
    "json-equals": (f"a\\u003d1\\u0026code\\u003d{SECRET}", "code\\u003d"),
    "json-token": (f"cb?access_token\\u003d{SECRET}", "access_token\\u003d"),
    "json-after-equals": (f"next\\u003dcode\\u003d{SECRET}", "next\\u003dcode"),
    "json-after-query": (f"cb\\u003fcode\\u003d{SECRET}", "cb\\u003fcode"),
    "json-escaped-value": (ESCAPED_VALUE, '"next": "e1"'),
    "json-escaped-slash": (f'{{"code": "4\\/{SECRET}"}}', '"code": "'),
    "json-escaped-backslash": (ESCAPED_BACKSLASH, '"next": "e1"'),
    "double-amp": (f"x%253D1%2526code%253D{SECRET}", "%2526code%253D"),
    "double-query": (f"%253Fcode%253D{SECRET}%2526y%253D2", "%2526y%253D2"),
}
_CASES = {
    "json-body": (lambda: _issue_request(BODY), '"endpoint": "e1"'),
    "authorization-header": (lambda: _issue_request("", AUTH), "'Accept'"),
    "query-http": (_urllib3_get, '"GET /api/contents/x?authuser=0&colab-'),
    "exception": (lambda: LOG.error("x", exc_info=ERROR), "Max retries"),
    "kernel-stdin": (_kernel_stdin_reply, "ready"),
    **{
        name: (functools.partial(LOG.debug, text), neighbour)
        for name, (text, neighbour) in _LOGGED_TEXT.items()
    },
}


@pytest.mark.parametrize("emit, neighbour", _CASES.values(), ids=_CASES.keys())
def test_credentials_are_redacted_from_log(read_log, emit, neighbour):
    emit()
    log = read_log()
    assert neighbour in log
    assert SECRET not in log


@pytest.mark.parametrize("case", ["json-body", "authorization-header"])
def test_credentials_are_redacted_from_stderr(home, capsys, case):
    setup_logging(True)
    emit, neighbour = _CASES[case]
    emit()
    err = capsys.readouterr().err
    assert neighbour in err
    assert SECRET not in err


def test_request_lines_survive_redaction(read_log):
    _issue_request()
    _urllib3_get()
    log = read_log()
    assert "Request: GET https://colab.research.google.com/tun/m/" in log
    assert "Response: 200 OK" in log
    assert '-token=REDACTED HTTP/1.1" 200 2' in log


def test_look_alikes_survive_redaction(read_log):
    line = ", ".join(LOOK_ALIKES)
    LOG.debug(line)
    assert line in read_log()
