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
import signal
import sys
import time
from typing import Callable, Optional, TypeVar

import typer

from colab_cli.auth import AuthProvider, get_credentials
from colab_cli.client import Client, Prod
from colab_cli.history import HistoryLogger
from colab_cli.state import SessionState, StateStore, SettingsStore
from colab_cli.utils import is_runtime_proxy_error


T = TypeVar("T")


class State:
    def __init__(self):
        self.client_oauth_config = os.path.expanduser("~/.colab-cli-oauth-config.json")
        self.config_path = None
        self.logtostderr = False
        self.auth_provider = AuthProvider.OAUTH2
        self._client = None
        self._store = None
        self._settings_store = None
        self._history = None
        self._sessions = None

    @property
    def store(self):
        if self._store is None:
            self._store = StateStore(self.config_path)
        return self._store

    @property
    def settings_store(self):
        if self._settings_store is None:
            # We don't currently allow overriding settings path via CLI,
            # but we could if needed. For now, use default.
            self._settings_store = SettingsStore()
        return self._settings_store

    @property
    def history(self):
        if self._history is None:
            self._history = HistoryLogger()
        return self._history

    @property
    def client(self):
        if self._client is None:
            creds = get_credentials(
                self.client_oauth_config, provider=self.auth_provider
            )
            self._client = Client(Prod(), creds)
        return self._client

    def _remove_session(self, name: str, endpoint: str) -> bool:
        """Removes a binding only if it still refers to ``endpoint``."""
        s = self.store.remove_if_endpoint(name, endpoint)
        if s is None:
            return False
        if s.keep_alive_pid:
            kill_process(s.keep_alive_pid)
        if self._sessions and name in self._sessions:
            del self._sessions[name]
        self.history.log_event(name, "session_terminated", {"reason": "pruned"})
        return True

    def _refresh_session_from_assignments(
        self,
        name: str,
        assignments,
        expected_session: Optional[SessionState] = None,
    ):
        """Reconciles one local binding with a known server-side snapshot."""
        s = expected_session or self.store.get(name)
        if s is None:
            return None
        assignment = next(
            (
                candidate
                for candidate in assignments
                if candidate.endpoint == s.endpoint
            ),
            None,
        )
        if assignment is None:
            if self._remove_session(name, s.endpoint):
                return None
            # Another process replaced the name after this server snapshot
            # began. The snapshot says nothing about that new endpoint, so
            # preserve it and let a later resolution reconcile it.
            current = self.store.get(name)
            if current is not None and self._sessions is not None:
                self._sessions[name] = current
            return current

        rpi = assignment.runtime_proxy_info
        refreshed = self.store.update_fields(
            name, s.endpoint, token=rpi.token, url=rpi.url
        )
        if refreshed is None:
            # A same-name replacement won the endpoint guard. Do not apply
            # credentials from the old endpoint to it.
            refreshed = self.store.get(name)
        if refreshed is not None and self._sessions is not None:
            self._sessions[name] = refreshed
        return refreshed

    def refresh_session(
        self,
        name: str,
        expected_session: Optional[SessionState] = None,
        timeout: Optional[float] = None,
    ):
        """Fetches and adopts the current runtime-proxy credentials."""
        s = expected_session or self.store.get(name)
        if s is None:
            return None
        request_kwargs = {"timeout": timeout} if timeout is not None else {}
        return self._refresh_session_from_assignments(
            name,
            self.client.list_assignments(**request_kwargs),
            expected_session=s,
        )

    def prune_session(self, name: str) -> bool:
        """Prunes only after the control plane confirms the assignment is gone.

        If the check is inconclusive, preserve the binding: deleting local
        access to a potentially live, billable VM is the worse failure mode.
        """
        s = self.store.get(name)
        if s is None:
            return False
        try:
            refreshed = self.refresh_session(name, expected_session=s)
        except Exception:
            return False
        return refreshed is None

    def run_with_runtime_proxy_retry(
        self,
        name: str,
        operation: Callable[[SessionState], T],
        initial_session: Optional[SessionState] = None,
    ) -> T:
        """Runs an operation and retries once with fresh proxy credentials."""
        s = initial_session or self.store.get(name)
        if s is None:
            raise RuntimeError(f"Session '{name}' not found")
        try:
            return operation(s)
        except Exception as error:
            if not is_runtime_proxy_error(error):
                raise
            old_credentials = (s.token, s.url)
            try:
                refreshed = self.refresh_session(name, expected_session=s)
            except Exception:
                raise error
            if (
                refreshed is None
                or refreshed.endpoint != s.endpoint
                or (refreshed.token, refreshed.url) == old_credentials
            ):
                raise error
            return operation(refreshed)

    def sync_sessions(self):
        # Check local store first. If it's empty, we don't necessarily need to hit the backend
        # unless we are specifically looking for server-side assignments (e.g. 'colab sessions').
        local_sessions = self.store.list()
        if not local_sessions:
            self._sessions = {}
            # We still need to return assignments for 'colab sessions' to work
            # But we only trigger client creation (and thus auth) if we have to.
            try:
                assignments = self.client.list_assignments()
            except SystemExit:
                # If auth fails, we just return empty assignments
                assignments = []
            return self._sessions, assignments

        assignments = self.client.list_assignments()
        self._sessions = local_sessions
        pruned = 0
        for name, s in list(self._sessions.items()):
            if (
                self._refresh_session_from_assignments(
                    name, assignments, expected_session=s
                )
                is None
            ):
                pruned += 1

        if pruned > 0:
            typer.echo(f"[colab] Pruned {pruned} stale local session(s).")

        return self._sessions, assignments

    def resolve_session(self, session_name: Optional[str]) -> str:
        if session_name:
            s = self.store.get(session_name)
            if s is not None:
                try:
                    self.refresh_session(session_name, expected_session=s)
                except Exception:
                    # The runtime path may still be reachable when the control
                    # plane is temporarily unavailable. Preserve and try it.
                    pass
            return session_name

        # Check local store first to avoid hitting the backend (and triggering auth) if we don't have to
        local_sessions = self.store.list()
        if not local_sessions:
            typer.echo(
                "[colab] Error: No active sessions found. Create one with 'colab new'."
            )
            raise typer.Exit(1)

        # If we have local sessions, we need to sync to make sure they are still valid.
        # This will trigger auth if valid credentials are not present.
        sessions, _ = self.sync_sessions()
        active_names = list(sessions.keys())

        if len(active_names) == 1:
            name = active_names[0]
            typer.echo(f"[colab] Using unique session '{name}'.")
            return name
        elif len(active_names) > 1:
            typer.echo(
                f"[colab] Error: Multiple active sessions found. Specify one with -s: {', '.join(active_names)}"
            )
            raise typer.Exit(1)
        else:
            typer.echo(
                "[colab] Error: No active sessions found. Create one with 'colab new'."
            )
            raise typer.Exit(1)


state = State()


def kill_process(pid: int):
    """Safely terminates a process by PID."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        # Give it a moment to exit
        for _ in range(5):
            time.sleep(0.1)
            os.kill(pid, 0)
    except OSError:
        # Already dead
        pass
    except Exception:
        logging.debug(f"Failed to kill process {pid}")


def setup_logging(log_to_stderr: bool):
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    requests_log = logging.getLogger("urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

    log_dir = os.path.expanduser("~/.config/colab-cli")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(log_dir, "colab.log"))
    file_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(file_handler)

    if log_to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(stream_handler)
