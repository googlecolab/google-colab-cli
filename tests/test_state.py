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

import multiprocessing
import os
import pytest
import tempfile
import threading
from datetime import datetime

import filelock

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
    assert settings.enable_update_check is True
    assert settings.last_check is None


def test_settings_store_save_load(temp_config):
    store = SettingsStore(temp_config)
    settings = Settings(enable_update_check=False, last_check=datetime(2026, 1, 1))
    store.save(settings)

    loaded = SettingsStore(temp_config).load()
    assert loaded.enable_update_check is False
    assert loaded.last_check == datetime(2026, 1, 1)


# --- Cross-platform locking (filelock ReadWriteLock) behavior ---------------


def test_lock_path_is_derived_from_path(temp_config):
    """The lock sidecar file lives next to the data file with a .lock suffix."""
    store = StateStore(temp_config)
    assert store.lock_path == temp_config + ".lock"


def test_store_uses_readwrite_lock_on_sidecar(temp_config):
    """The store holds a ReadWriteLock bound to the sidecar path."""
    store = StateStore(temp_config)
    assert isinstance(store._rwlock, filelock.ReadWriteLock)
    assert store._rwlock.lock_file == temp_config + ".lock"


def test_separate_store_instances_can_write_from_threads(temp_config):
    """Two StateStore instances writing from different threads must not crash.

    Regression guard for filelock's is_singleton=True default: it merges two
    same-path ReadWriteLock objects into one reentrant lock whose reentrancy
    guard raises RuntimeError when two threads contend for the write lock. We
    pass is_singleton=False so they serialize via the underlying file lock.
    """
    errors = []

    def writer(start):
        store = StateStore(temp_config)
        try:
            for i in range(start, start + 25):
                store.add(SessionState(name=f"n{i}", token="t", url="u", endpoint="e"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(s,)) for s in (0, 100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(StateStore(temp_config).list()) == 50


def test_write_acquires_write_lock(temp_config, mocker):
    """Writes go through the ReadWriteLock's exclusive write_lock()."""
    store = StateStore(temp_config)
    spy = mocker.spy(store._rwlock, "write_lock")
    store.add(SessionState(name="s", token="t", url="u", endpoint="e"))
    assert spy.call_count >= 1
    assert store.get("s") is not None


def test_read_acquires_read_lock(temp_config, mocker):
    """Reads go through the ReadWriteLock's shared read_lock()."""
    store = StateStore(temp_config)
    store.add(SessionState(name="s", token="t", url="u", endpoint="e"))

    spy = mocker.spy(store._rwlock, "read_lock")
    assert store.get("s") is not None
    assert spy.call_count >= 1


def test_uses_filelock_not_fcntl(temp_config):
    """The store must lock via the platform-independent filelock library.

    Guards against a regression back to the POSIX-only fcntl.flock approach
    that broke Windows users.
    """
    store = StateStore(temp_config)
    assert store._rwlock.__class__.__module__.startswith("filelock")


def test_no_fcntl_import_in_state_module():
    """The state module must not depend on the POSIX-only fcntl module."""
    import colab_cli.state as state_module

    assert not hasattr(state_module, "fcntl")


def _mp_hold_write_lock(lock_path, hold_for, acquired_evt, release_evt):
    rw = filelock.ReadWriteLock(lock_path)
    with rw.write_lock():
        acquired_evt.set()
        release_evt.wait(timeout=hold_for)


def test_write_lock_blocks_across_processes(temp_config):
    """A write lock held by another process must block the store's write section.

    The cross-process guarantee is the whole point of the fcntl->filelock
    switch, so we hold the write lock from a separate process and confirm the
    in-process writer cannot proceed until it's released.
    """
    store = StateStore(temp_config)
    # Pre-create the data file so add() doesn't race on first creation.
    store.add(SessionState(name="seed", token="t", url="u", endpoint="e"))

    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_mp_hold_write_lock, args=(store.lock_path, 10, acquired, release)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=5), "holder process never acquired the lock"

        finished = threading.Event()

        def writer():
            store.add(SessionState(name="blocked", token="t", url="u", endpoint="e"))
            finished.set()

        t = threading.Thread(target=writer)
        t.start()

        # While the external process holds the write lock, the writer can't finish.
        assert not finished.wait(timeout=0.75)

        release.set()
        assert finished.wait(timeout=5)
        t.join()
        assert store.get("blocked") is not None
    finally:
        release.set()
        holder.join(timeout=5)


def _mp_hold_read_lock(lock_path, acquired_evt, release_evt):
    rw = filelock.ReadWriteLock(lock_path)
    with rw.read_lock():
        acquired_evt.set()
        release_evt.wait(timeout=10)


def test_readers_are_concurrent_across_processes(temp_config):
    """Shared read locks must allow concurrent readers (the LOCK_SH semantics).

    This is the capability ReadWriteLock buys us over a plain exclusive
    FileLock: while one process holds a read lock, this process can still read.
    """
    store = StateStore(temp_config)
    store.add(SessionState(name="s", token="t", url="u", endpoint="e"))

    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_mp_hold_read_lock, args=(store.lock_path, acquired, release)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=5), "holder process never acquired the read lock"

        done = threading.Event()
        result = {}

        def reader():
            result["value"] = store.get("s")
            done.set()

        t = threading.Thread(target=reader)
        t.start()

        # The read must complete even though another process holds a read lock.
        assert done.wait(timeout=3), "concurrent read was blocked by another reader"
        t.join()
        assert result["value"] is not None
    finally:
        release.set()
        holder.join(timeout=5)


def _mp_add_sessions(path, start, count):
    store = StateStore(path)
    for i in range(start, start + count):
        store.add(SessionState(name=f"p{i}", token="t", url="u", endpoint="e"))


def test_state_store_multiprocess_concurrency(temp_config):
    """filelock must serialize writes across separate processes (not just threads).

    fcntl.flock is per-open-file-description and advisory; this test exercises
    the cross-process guarantee that motivated the switch.
    """
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_mp_add_sessions, args=(temp_config, 0, 40))
    p2 = ctx.Process(target=_mp_add_sessions, args=(temp_config, 40, 40))

    p1.start()
    p2.start()
    p1.join()
    p2.join()

    assert p1.exitcode == 0
    assert p2.exitcode == 0
    assert len(StateStore(temp_config).list()) == 80
