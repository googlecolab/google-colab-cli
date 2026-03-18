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

import os
import pytest
import tempfile
import threading
from datetime import datetime
from colab_cli.state import StateStore, SessionState, SettingsStore, Settings


@pytest.fixture
def temp_config():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_state_store_add_get(temp_config):
    store = StateStore(temp_config)
    state = SessionState(
        name="test-session",
        token="token123",
        url="http://localhost",
        endpoint="endpoint456",
        variant="TPU",
        accelerator="V5E1",
    )
    store.add(state)

    # Reload store
    new_store = StateStore(temp_config)
    loaded = new_store.get("test-session")
    assert loaded is not None
    assert loaded.name == "test-session"
    assert loaded.token == "token123"
    assert loaded.variant == "TPU"


def test_state_store_remove(temp_config):
    store = StateStore(temp_config)
    state = SessionState(name="to-be-removed", token="tok", url="url", endpoint="end")
    store.add(state)
    assert store.get("to-be-removed") is not None

    store.remove("to-be-removed")
    assert store.get("to-be-removed") is None

    # Reload check
    new_store = StateStore(temp_config)
    assert new_store.get("to-be-removed") is None


def test_state_store_list(temp_config):
    store = StateStore(temp_config)
    s1 = SessionState(name="s1", token="t1", url="u1", endpoint="e1")
    s2 = SessionState(name="s2", token="t2", url="u2", endpoint="e2")
    store.add(s1)
    store.add(s2)

    sessions = store.list()
    assert len(sessions) == 2
    assert "s1" in sessions
    assert "s2" in sessions


def test_state_store_invalid_json(temp_config):
    with open(temp_config, "w") as f:
        f.write("invalid json")

    store = StateStore(temp_config)
    assert store.list() == {}


def test_state_store_concurrency(temp_config):
    def add_sessions(start, count, path):
        store = StateStore(path)
        for i in range(start, start + count):
            s = SessionState(name=f"s{i}", token="t", url="u", endpoint="e")
            store.add(s)

    t1 = threading.Thread(target=add_sessions, args=(0, 50, temp_config))
    t2 = threading.Thread(target=add_sessions, args=(50, 50, temp_config))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    new_store = StateStore(temp_config)
    # This might pass or fail depending on luck without locking
    # But usually it fails with 100 iterations.
    assert len(new_store.list()) == 100


def test_settings_store_defaults(temp_config):
    store = SettingsStore(temp_config)
    settings = store.load()
    assert settings.enable_auto_update is True
    assert settings.last_check is None


def test_settings_store_save_load(temp_config):
    store = SettingsStore(temp_config)
    settings = Settings(enable_auto_update=False, last_check=datetime(2026, 1, 1))
    store.save(settings)

    loaded = SettingsStore(temp_config).load()
    assert loaded.enable_auto_update is False
    assert loaded.last_check == datetime(2026, 1, 1)
