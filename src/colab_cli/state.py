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

import contextlib
import json
import os
from datetime import datetime
from typing import Dict, Optional, Tuple, Iterator, IO

import filelock
from pydantic import BaseModel


class SessionState(BaseModel):
    name: str
    token: str
    url: str
    endpoint: str
    variant: str = "DEFAULT"
    accelerator: str = "NONE"
    kernel_id: Optional[str] = None
    session_id: Optional[str] = None
    last_execution: Optional[Tuple[str, Optional[str], str]] = None
    running: Optional[str] = None
    keep_alive_pid: Optional[int] = None


class Settings(BaseModel):
    update_url: str = "https://pypi.org/pypi/google-colab-cli/json"
    last_check: Optional[datetime] = None
    enable_update_check: bool = True
    # Highest version seen on the update source; cached for the banner.
    latest_version: Optional[str] = None


class _LockedFileStore:
    def __init__(self, path: str):
        self.path = path
        self.lock_path = "%s.lock" % self.path
        # ReadWriteLock gives us shared (concurrent) readers and exclusive
        # writers -- the cross-platform equivalent of fcntl LOCK_SH/LOCK_EX.
        # is_singleton=False keeps each store's lock independent: with the
        # default (True), two StateStore instances for the same path in one
        # process are merged into a single reentrant lock, whose reentrancy
        # guard then raises RuntimeError when two threads contend for the write
        # lock. We want them to actually serialize via the underlying file lock.
        self._rwlock = filelock.ReadWriteLock(self.lock_path, is_singleton=False)
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _write_data(self, f: IO, data: str):
        f.seek(0)
        f.truncate()
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    @contextlib.contextmanager
    def _lock_shared(self) -> Iterator[Optional[IO]]:
        if not os.path.exists(self.path):
            yield None
            return
        with self._rwlock.read_lock():
            with open(self.path, "r") as f:
                yield f

    @contextlib.contextmanager
    def _lock_exclusive(self) -> Iterator[IO]:
        with self._rwlock.write_lock():
            with open(self.path, "a+") as f:
                yield f


class SettingsStore(_LockedFileStore):
    def __init__(self, path: Optional[str] = None):
        if not path:
            path = os.path.expanduser("~/.config/colab-cli/settings.json")
        super().__init__(path)

    def load(self) -> Settings:
        with self._lock_shared() as f:
            if f is None:
                return Settings()
            try:
                content = f.read()
                if not content or content.isspace():
                    return Settings()
                data = json.loads(content)
                return Settings.model_validate(data)
            except Exception:
                return Settings()

    def save(self, settings: Settings):
        with self._lock_exclusive() as f:
            self._write_data(f, settings.model_dump_json(indent=2))


class StateStore(_LockedFileStore):
    def __init__(self, path: Optional[str] = None):
        if not path:
            path = os.path.expanduser("~/.config/colab-cli/sessions.json")
        super().__init__(path)

    def _load_raw(self, f) -> Dict[str, SessionState]:
        try:
            f.seek(0)
            content = f.read()
            if not content or content.isspace():
                return {}
            data = json.loads(content)
            return {k: SessionState(**v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_raw(self, f, sessions: Dict[str, SessionState]):
        content = json.dumps({k: v.model_dump() for k, v in sessions.items()}, indent=2)
        self._write_data(f, content)

    def add(self, state: SessionState):
        with self._lock_exclusive() as f:
            sessions = self._load_raw(f)
            sessions[state.name] = state
            self._save_raw(f, sessions)

    def get(self, name: str) -> Optional[SessionState]:
        with self._lock_shared() as f:
            if f is None:
                return None
            sessions = self._load_raw(f)
            return sessions.get(name)

    def remove(self, name: str):
        with self._lock_exclusive() as f:
            sessions = self._load_raw(f)
            if name in sessions:
                del sessions[name]
                self._save_raw(f, sessions)

    def list(self) -> Dict[str, SessionState]:
        with self._lock_shared() as f:
            if f is None:
                return {}
            return self._load_raw(f)
