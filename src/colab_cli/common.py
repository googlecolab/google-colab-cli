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
import logging
import os
import re
import shlex
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import NoReturn, Optional

import typer

from colab_cli.auth import AuthProvider, get_credentials
from colab_cli.client import Client, Prod, RuntimeProxyInfo
from colab_cli.history import HistoryLogger
from colab_cli.state import (
    SessionState,
    StateStore,
    SettingsStore,
    make_private,
    private_opener,
)

# Headroom so a token doesn't expire mid-command.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def _apply_proxy_info(s: SessionState, info: RuntimeProxyInfo):
    s.token = info.token
    s.url = info.url
    s.token_expires_at = info.expires_at()


def exit_ssh_only(name: str, more: str = "") -> NoReturn:
    """Prints an ssh-only error for an attached session and exits.

    Args:
      name: Session name the caller tried to use.
      more: Optional trailing text appended after the ``colab ssh`` command.

    Raises:
      typer.Exit: Always exits with code 1.
    """
    typer.echo(
        f"[colab] Error: Session '{name}' uses a runtime the CLI did not"
        f" create. Use it with `colab ssh -s {shlex.quote(name)}`{more}.",
        err=True,
    )
    raise typer.Exit(1)


def exit_attached_runtime(name: str, attached_name: str) -> NoReturn:
    """Prints an error for a CLI session sharing an attached runtime and exits.

    Args:
      name: CLI session name that shares the attached endpoint.
      attached_name: Attached session name that owns the runtime locally.

    Raises:
      typer.Exit: Always exits with code 1.
    """
    ssh_cmd = f"`colab ssh -s {shlex.quote(attached_name)}`"
    stop_cmd = f"`colab stop -s {shlex.quote(name)}`"
    typer.echo(
        f"[colab] Error: Session '{name}' uses the same runtime as attached"
        f" session '{attached_name}'. Use {ssh_cmd}, or remove '{name}' with"
        f" {stop_cmd}.",
        err=True,
    )
    raise typer.Exit(1)


def _exit_only_attached(sessions: Mapping[str, SessionState]) -> NoReturn:
    attached = [n for n, s in sessions.items() if s.attached is True]
    first, *others = sorted(attached or sessions)
    also = f" (also attached: {', '.join(others)})" if others else ""
    remove = f", or remove it with `colab stop -s {shlex.quote(first)}`"
    exit_ssh_only(first, remove + also)


def _is_attached_session(store: StateStore, s: SessionState) -> bool:
    return (
        s.attached is True or store.attached_name_for_endpoint(s.endpoint) is not None
    )


class State:
    def __init__(self):
        self.client_oauth_config = os.path.expanduser("~/.colab-cli-oauth-config.json")
        self.config_path: str | None = None
        self.logtostderr = False
        self.auth_provider = AuthProvider.OAUTH2
        self.use_public_api: Optional[bool] = None
        self._client = None
        self._store = None
        self._settings_store = None
        self._history = None
        self._sessions = None

    @property
    def store(self):
        if self._store is None:
            path = self.config_path or os.path.join(
                os.path.dirname(self.settings_store.path), "sessions.json"
            )
            self._store = StateStore(path)
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
            use_public = (
                self.use_public_api
                if self.use_public_api is not None
                else self.settings_store.load().use_public_api
            )
            self._client = Client(
                Prod(),
                creds,
                use_public_api=use_public,
                settings_store=self.settings_store,
                is_attached=lambda ep: self.store.is_attached_endpoint(ep),
            )
        return self._client

    def refuse_attached_name(self, name: str) -> None:
        """Exits if ``name`` already refers to an attached session in the store.

        Args:
          name: Candidate session name for ``colab new`` or ``colab run``.

        Raises:
          typer.Exit: With code 1 if ``name`` is an attached session.
        """
        record = self.store.get(name)
        if record is not None and record.attached is True:
            remove = f", remove it with `colab stop -s {shlex.quote(name)}`"
            exit_ssh_only(name, f"{remove}, or pick another name")

    def prune_session(self, name: str):
        """Removes a session from local state."""
        self.store.remove(name)
        if self._sessions and name in self._sessions:
            del self._sessions[name]
        self.history.log_event(name, "session_terminated", {"reason": "pruned"})

    def get_session(
        self,
        name: str,
        *,
        ignore_missing_session: bool = False,
        allow_attached: bool = False,
    ) -> Optional[SessionState]:
        """Load a session, refreshing its runtime proxy token if it's near expiry.

        A session is missing if it's unknown locally or its assignment is gone
        server-side (in which case it's pruned). Missing sessions print an error
        and exit, unless ignore_missing_session is set, in which case this
        returns None.

        Args:
          name: Session name to load from the local store.
          ignore_missing_session: Return None instead of exiting when missing.
          allow_attached: Allow loading attached sessions and CLI records
            whose endpoint is attached (for ``ssh``, ``stop``, and teardown).

        Returns:
          The loaded ``SessionState``, or None when missing and
          ``ignore_missing_session`` is True.

        Raises:
          typer.Exit: With code 1 if missing (and not ignored) or refused as
            ssh-only.
        """
        s = self.store.get(name)
        if s and not allow_attached:
            if s.attached is True:
                exit_ssh_only(name)
            attached_name = self.store.attached_name_for_endpoint(s.endpoint)
            if attached_name is not None:
                exit_attached_runtime(name, attached_name)
        if s and (
            not s.token_expires_at
            or s.token_expires_at - datetime.now(timezone.utc) <= TOKEN_REFRESH_MARGIN
        ):
            by_endpoint = {a.endpoint: a for a in self.client.list_assignments()}
            if s.endpoint in by_endpoint:
                _apply_proxy_info(s, by_endpoint[s.endpoint].runtime_proxy_info)
                self.store.add(s)
            else:
                self.prune_session(name)
                s = None

        if s is None and not ignore_missing_session:
            typer.echo(f"[colab] Session '{name}' not found.")
            raise typer.Exit(1)
        return s

    def sync_sessions(self):
        if self._sessions is not None:
            return self._sessions, self.client.list_assignments()

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
        by_endpoint = {a.endpoint: a for a in assignments}

        self._sessions = local_sessions
        pruned = 0
        for name, s in list(self._sessions.items()):
            if s.endpoint not in by_endpoint:
                self.prune_session(name)
                pruned += 1
            else:
                _apply_proxy_info(s, by_endpoint[s.endpoint].runtime_proxy_info)
                self.store.add(s)

        if pruned > 0:
            typer.echo(f"[colab] Pruned {pruned} stale local session(s).")

        return self._sessions, assignments

    def resolve_session(
        self, session_name: str | None, *, allow_attached: bool = False
    ) -> str:
        """Resolves a target session name or selects the sole non-attached one.

        Args:
          session_name: Explicit session name, or None to auto-select when
            exactly one eligible active session exists.
          allow_attached: Whether attached sessions (and CLI records sharing an
            attached endpoint) are eligible for auto-selection.

        Returns:
          The resolved session name.

        Raises:
          typer.Exit: With code 1 if no eligible active sessions exist, multiple
            eligible sessions remain, or only attached sessions exist when
            ``allow_attached`` is False.
        """
        if session_name:
            return session_name

        # Check local store first to avoid hitting the backend (and triggering auth) if we don't have to
        local_sessions = self.store.list()
        if not local_sessions:
            typer.echo(
                "[colab] Error: No active sessions found. Create one with 'colab new'."
            )
            raise typer.Exit(1)
        if not allow_attached and all(
            _is_attached_session(self.store, s) for s in local_sessions.values()
        ):
            _exit_only_attached(local_sessions)

        # If we have local sessions, we need to sync to make sure they are still valid.
        # This will trigger auth if valid credentials are not present.
        sessions, _ = self.sync_sessions()
        active_names = [
            n
            for n, s in sessions.items()
            if allow_attached or not _is_attached_session(self.store, s)
        ]

        if len(active_names) == 1:
            name = active_names[0]
            typer.echo(f"[colab] Using unique session '{name}'.")
            return name
        if len(active_names) > 1:
            typer.echo(
                f"[colab] Error: Multiple active sessions found. Specify one with -s: {', '.join(active_names)}"
            )
            raise typer.Exit(1)
        if sessions and not allow_attached:
            _exit_only_attached(sessions)
        typer.echo(
            "[colab] Error: No active sessions found. Create one with 'colab new'."
        )
        raise typer.Exit(1)


state = State()

_SECRET_FIELD_PATTERN = r"(?:[\w-]*token|code|code_verifier|client_secret)"
# Each alternative has exactly one capturing group, the prefix to keep:
# _RedactingFormatter keeps m.group(m.lastindex) and masks the rest.
# Grouped value repeats are possessive (++) so long values use constant memory.
_SECRET_RE = re.compile(
    rf"""
    # Credential header in a dict, tuple, bytes or raw line; keeps the scheme.
    (\b(?:authorization|cookie|set-cookie|x-colab-runtime-proxy-token
        |x-goog-colab-token)(?:['"]?\s*:|['"]\s*,)\s*(?:b?['"])?
        (?:(?:bearer|basic)\s+)?)[^'"\r\n]+
    # Quoted token key in JSON or a dict repr, also bytes or escaped JSON; the
    # value runs through JSON escapes like \/ up to a quote, escaped or not.
    |((?<![\w-])b?\\?['"]{_SECRET_FIELD_PATTERN}\\?['"]\s*:\s*b?\\?['"])
        (?:[^'"\\]|\\[^'"])++
    # Token key in a URL query or form body, also percent- or JSON-escaped
    # (%3D, %253D, \u003d), at a word start or after %XX, %25XX or \uXXXX.
    |((?:(?<![\w-])|(?<=%[0-9a-f]{{2}})|(?<=%25[0-9a-f]{{2}})
        |(?<=\\u[0-9a-f]{{4}}))
        {_SECRET_FIELD_PATTERN}(?:=|%(?:25)?3D|\\u003d))
        (?:[^&\s'"#%]|%(?!(?:25)?26)[0-9a-f]{{2}})++
    # Bearer or Basic credential anywhere in free text.
    |(\b(?:bearer|basic)\s+)[\w.~+/=-]{{8,}}
    """,
    re.IGNORECASE | re.VERBOSE,
)


class _RedactingFormatter(logging.Formatter):
    """Formatter that masks tokens, auth headers and cookies in its output."""

    def format(self, record: logging.LogRecord) -> str:
        return _SECRET_RE.sub(
            lambda m: m.group(m.lastindex) + "REDACTED", super().format(record)
        )


def setup_logging(log_to_stderr: bool):
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    requests_log = logging.getLogger("urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True
    # Its DEBUG output includes stdin replies to the kernel, such as passwords.
    logging.getLogger("jupyter_kernel_client").setLevel(logging.INFO)

    log_dir = os.path.dirname(state.settings_store.path)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "colab.log")
    # FileHandler can't set permissions, so create the file owner-only first.
    with contextlib.suppress(OSError):
        open(log_path, "a", opener=private_opener).close()
    make_private(log_path)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(_RedactingFormatter(log_format))
    logger.addHandler(file_handler)

    if log_to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(_RedactingFormatter(log_format))
        logger.addHandler(stream_handler)
