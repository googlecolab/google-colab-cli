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
from typing import Dict, Optional, Set, Tuple, Iterator, IO

import filelock
from pydantic import BaseModel


class SessionState(BaseModel):
    name: str
    token: str
    url: str
    endpoint: str
    token_expires_at: Optional[datetime] = None
    variant: str = "DEFAULT"
    accelerator: str = "NONE"
    machine_shape: str = "STANDARD"
    kernel_id: Optional[str] = None
    session_id: Optional[str] = None
    last_execution: Optional[Tuple[str, Optional[str], str]] = None
    running: Optional[str] = None
    attached: bool = False


class Settings(BaseModel):
    update_url: str = "https://pypi.org/pypi/google-colab-cli/json"
    last_check: Optional[datetime] = None
    enable_update_check: bool = True
    # Highest version seen on the update source; cached for the banner.
    latest_version: Optional[str] = None


_OWNER_ONLY_MODE = 0o600


def private_opener(path: str, flags: int) -> int:
    """Opens `path` for `open(opener=...)` and returns its file descriptor.

    New files are created readable only by the owner.
    """
    return os.open(path, flags, _OWNER_ONLY_MODE)


def make_private(path: str) -> None:
    """Restricts an existing file to owner-only access, ignoring errors."""
    with contextlib.suppress(OSError):
        os.chmod(path, _OWNER_ONLY_MODE)


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
        make_private(self.path)

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
            with open(self.path, "a+", opener=private_opener) as f:
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


_ATTACHED_SUFFIX = ".attached.json"


class StoreError(Exception):
    """A session store problem that the CLI reports as an error message."""


class AttachedStorePathError(StoreError):
    """The sessions path, or its attached path, aliases the other kind."""

    def __init__(self, path: str, reason: str = ""):
        if not reason:
            reason = "it is an attached sessions file; pass a sessions file"
        super().__init__(f"Cannot use '{path}' as the sessions file: {reason}.")


def resolve_store_paths(path: Optional[str] = None) -> Tuple[str, str]:
    """Returns the sessions file and its attached file for `path`.

    Hard links and copies of either file are not detected.

    Args:
      path: The sessions file; None or empty means
        ~/.config/colab-cli/sessions.json.

    Raises:
      AttachedStorePathError: If one store's attached file would be read as
        another store's sessions file, or the reverse.
    """
    if not path:
        path = os.path.expanduser("~/.config/colab-cli/sessions.json")
    for name in (path, os.path.realpath(path)):
        if name.lower().endswith(_ATTACHED_SUFFIX):
            raise AttachedStorePathError(path)
    attached_path = os.path.splitext(path)[0] + _ATTACHED_SUFFIX
    resolved = os.path.realpath(attached_path).lower()
    if not resolved.endswith(_ATTACHED_SUFFIX):
        raise AttachedStorePathError(
            path, f"'{attached_path}' links to a file of another kind"
        )
    return path, attached_path


class DuplicateSessionError(StoreError):
    """A session name is in both the sessions file and the attached file."""

    def __init__(self, name: str):
        super().__init__(
            f"Session '{name}' is recorded both as a CLI session and as an "
            f"attached session. Run 'colab stop -s {name}' to forget the "
            "attached record."
        )


class StateStore(_LockedFileStore):
    """Session records: CLI-created ones in `path`, attached ones beside it.

    Attached records live in their own file so that older CLI versions, which
    read only `path`, never see them and so never unassign their runtimes.
    """

    def __init__(self, path: Optional[str] = None):
        path, attached_path = resolve_store_paths(path)
        super().__init__(path)
        self._attached = _LockedFileStore(attached_path)
        make_private(self._attached.lock_path)

    def _load_raw(self, f, attached: bool) -> Dict[str, SessionState]:
        try:
            f.seek(0)
            content = f.read()
            if not content or content.isspace():
                return {}
            data = json.loads(content)
            origin = {"attached": attached}
            return {k: SessionState(**{**v, **origin}) for k, v in data.items()}
        except Exception:
            return {}

    def _save_raw(self, f, sessions: Dict[str, SessionState]):
        content = json.dumps(
            {k: v.model_dump(mode="json") for k, v in sessions.items()}, indent=2
        )
        self._write_data(f, content)

    def _read(self, store: _LockedFileStore) -> Dict[str, SessionState]:
        with store._lock_shared() as f:
            if f is None:
                return {}
            return self._load_raw(f, store is self._attached)

    def _remove_from(self, store: _LockedFileStore, name: str) -> None:
        if not os.path.exists(store.path):
            return
        with store._lock_exclusive() as f:
            sessions = self._load_raw(f, store is self._attached)
            if name in sessions:
                del sessions[name]
                self._save_raw(f, sessions)

    def add(self, state: SessionState):
        """Writes `state` to the file its `attached` flag names.

        A new record defaults to a CLI session, so writers of attached sessions
        set the flag.
        """
        store = self._attached if state.attached else self
        with store._lock_exclusive() as f:
            sessions = self._load_raw(f, state.attached)
            sessions[state.name] = state
            self._save_raw(f, sessions)

    def get(self, name: str) -> Optional[SessionState]:
        """Returns the record named `name` from either file, or None.

        Raises:
          DuplicateSessionError: If `name` is in both files.
        """
        own, attached_records = self._read(self), self._read(self._attached)
        if name in own and name in attached_records:
            raise DuplicateSessionError(name)
        return own.get(name) or attached_records.get(name)

    def remove(self, name: str):
        self._remove_from(self, name)
        self._remove_from(self._attached, name)

    def remove_attached(self, name: str) -> None:
        """Forgets only the attached record for `name`."""
        self._remove_from(self._attached, name)

    def remove_cli(self, name: str) -> None:
        """Removes only the CLI record for `name`."""
        self._remove_from(self, name)

    def attached_endpoints(self) -> Set[str]:
        """Returns the endpoints of the attached records."""
        return {s.endpoint for s in self._read(self._attached).values()}

    def list(self) -> Dict[str, SessionState]:
        """Returns the records of both files by name.

        Raises:
          DuplicateSessionError: If a name is in both files.
        """
        own, attached_records = self._read(self), self._read(self._attached)
        duplicates = own.keys() & attached_records.keys()
        if duplicates:
            raise DuplicateSessionError(min(duplicates))
        return {**own, **attached_records}
