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
import os
from unittest.mock import MagicMock

import pytest

from colab_cli.common import setup_logging
from colab_cli.history import HistoryLogger
from colab_cli.state import SessionState, Settings, SettingsStore, StateStore

LOGGERS = (None, "urllib3")
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
