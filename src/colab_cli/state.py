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
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, Iterator, IO
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
    update_url: str = (
        "https://raw.githubusercontent.com/googlecolab/colab-cli/main/version.json"
    )
    update_file_path: Path = Path(
        "/google/src/files/head/depot/google3/experimental/colab/colab-cli/releases/version.json"
    )
    last_check: Optional[datetime] = None
    enable_update_check: bool = True
    # Highest version seen across update sources; cached for the banner.
    latest_version: Optional[str] = None


class _LockedFileStore:
    def __init__(self, path: str):
        self.path = path
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
        with open(self.path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                yield f
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _lock_exclusive(self) -> Iterator[IO]:
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield f
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


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
