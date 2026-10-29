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
import logging
import os
from unittest.mock import MagicMock

import pytest
import requests

from colab_cli.client import Client, Prod
from colab_cli.commands.ssh import _build_ws_url
from colab_cli.common import setup_logging
from colab_cli.history import HistoryLogger
from colab_cli.state import SessionState, Settings, SettingsStore, StateStore

LOGGERS = (None, "urllib3", "jupyter_kernel_client")
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
SESSION = SessionState(name="s", token="t", url="https://h", endpoint="e")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    levels = {name: logging.getLogger(name).level for name in LOGGERS}
    yield tmp_path
    for handler in logging.getLogger().handlers:
        handler.close()
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def _save_settings(home):
    SettingsStore(f"{home}/settings.json").save(Settings())


_PRIVATE_FILES = {
    ".config/colab-cli/colab.log": lambda h: setup_logging(False),
    "sessions.json": lambda h: StateStore(f"{h}/sessions.json").add(SESSION),
    "settings.json": _save_settings,
    "s.jsonl": lambda h: HistoryLogger(str(h)).log_event("s", "x", {}),
}


@POSIX_ONLY
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "was-0644"])
@pytest.mark.parametrize("relative", list(_PRIVATE_FILES))
def test_files_are_private_to_the_user(home, relative, existing):
    path = home / relative
    umask = os.umask(0o022)
    try:
        if existing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(0o644)
        _PRIVATE_FILES[relative](home)
    finally:
        os.umask(umask)
    assert path.stat().st_mode & 0o777 == 0o600


@POSIX_ONLY
def test_existing_log_is_kept_and_made_private(home):
    path = home / ".config/colab-cli/colab.log"
    path.parent.mkdir(parents=True)
    path.write_text("earlier line\n")
    path.chmod(0o644)
    setup_logging(False)
    logging.getLogger("colab_cli").info("later line")
    assert path.read_text().startswith("earlier line\n")
    assert "later line" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


@POSIX_ONLY
def test_history_files_are_made_private_when_only_read(home):
    path = home / "old.jsonl"
    path.write_text('{"event_type": "x"}\n')
    path.chmod(0o644)
    assert HistoryLogger(str(home)).get_history("old")
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("relative", list(_PRIVATE_FILES))
def test_chmod_failure_is_not_fatal(home, monkeypatch, relative):
    monkeypatch.setattr(os, "chmod", MagicMock(side_effect=PermissionError))
    _PRIVATE_FILES[relative](home)
    assert (home / relative).exists()


SECRET = "sentinel-0123456789abcdef"
QUERY = f"/api/contents/x?authuser=0&colab-runtime-proxy-token={SECRET}"
BODY = json.dumps({"endpoint": "e1", "runtimeProxyInfo": {"token": SECRET}})
AUTH = {"Authorization": f"Bearer {SECRET}", "Accept": "text/html"}
PROXY_HEADERS = {"X-Colab-Runtime-Proxy-Token": SECRET, "X-Colab-Tunnel": "G"}
RAW_AUTH = f"Authorization: Bearer {SECRET}"
RAW_BASIC = f"Sending Basic {SECRET}"
BYTES_HEADERS = {b"Authorization": f"Bearer {SECRET}".encode(), b"Accept": b"*"}
KERNEL_WS = f"wss://h/api/kernels/k/channels?session_id=x&token={SECRET}"
SSH_WS = _build_ws_url(SESSION.model_copy(update={"token": SECRET}))
ERROR = requests.ConnectionError(f"Max retries exceeded: {QUERY}")
LOG = logging.getLogger("colab_cli.test")
JKC = logging.getLogger("jupyter_kernel_client")


@pytest.fixture
def read_log(home):
    setup_logging(False)
    return lambda: (home / ".config/colab-cli/colab.log").read_text()


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
    JKC.debug("input_reply %s", SECRET)
    JKC.info("ready")


_CASES = {
    "json-body": (lambda: _issue_request(BODY), '"endpoint": "e1"'),
    "authorization-header": (lambda: _issue_request("", AUTH), "'Accept'"),
    "authorization-raw": (lambda: LOG.debug(RAW_AUTH), "Authorization: Bearer"),
    "basic-credential": (lambda: LOG.debug(RAW_BASIC), "Sending Basic "),
    "bytes-header": (lambda: LOG.debug(BYTES_HEADERS), "b'Bearer "),
    "proxy-token-header": (lambda: LOG.debug(PROXY_HEADERS), "Tunnel"),
    "query-http": (_urllib3_get, '"GET /api/contents/x?authuser=0&colab-'),
    "query-wss-ssh": (lambda: LOG.debug(SSH_WS), "wss://h/colab/ssh?"),
    "query-wss-kernel": (lambda: LOG.debug(KERNEL_WS), "session_id=x&"),
    "exception": (lambda: LOG.error("x", exc_info=ERROR), "Max retries"),
    "kernel-stdin": (_kernel_stdin_reply, "ready"),
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
