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

"""Connect to a Colab runtime over the ``/colab/ssh`` WebSocket endpoint.

Modes:

* ``colab ssh``                    use your only active session (or auto-create
                                   one) and open an interactive shell in
                                   ``/content``.
* ``colab ssh -s SESSION``         same, targeting SESSION explicitly.
* ``colab ssh --proxy-mode -s S``  act as an OpenSSH ProxyCommand-compatible
                                   WebSocket-stdio bridge for ``~/.ssh/config``.

Every ``colab ssh`` flag also works in ``--proxy-mode``: with ``-s NAME`` the
session is created if it does not exist (so a config host works on first
connect), ``--gpu/--tpu`` pick the accelerator for that auto-created runtime,
and ``--rm`` stops the runtime when you disconnect.

``--identity/-i`` overrides the default key order (``~/.ssh/id_ed25519`` ->
``id_ecdsa``); the public key is derived via ``ssh-keygen -y -f`` and sent in
the ``X-Colab-Ssh-Pubkey`` header. RSA keys are rejected by the server, so
``id_rsa`` is not auto-selected.
"""

import contextlib
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
from typing import Optional
from urllib.parse import urlparse

from colab_cli.state import SessionState
import typer
from typing_extensions import Annotated
import websocket

_SSH_PATH = "/colab/ssh"
_KEY_TYPES = ["id_ed25519.pub", "id_ecdsa.pub"]  # ssh-rsa is rejected server-side
_PUBKEY_HEADER = "X-Colab-Ssh-Pubkey"
_SSH_HOST = "root@colab-runtime"
_DEFAULT_REMOTE_DIR = "/content"  # Colab's standard working dir; land here, not /root


def _resolve_pubkey(identity: Optional[str]) -> str:
    """Returns the public key to send in the ``X-Colab-Ssh-Pubkey`` header.

    With ``identity``, derives it from the private key via ``ssh-keygen -y -f``;
    otherwise scans ``~/.ssh`` for the first existing ``id_<type>.pub`` in
    preference order.
    """
    if identity:
        identity = os.path.expanduser(identity)
        if not os.path.exists(identity):
            typer.echo(f"[colab] --identity {identity}: file not found.", err=True)
            raise typer.Exit(code=2)
        try:
            res = subprocess.run(
                ["ssh-keygen", "-y", "-f", identity],
                check=True,
                capture_output=True,
                text=True,
            )
            pubkey = res.stdout.strip()
            if not pubkey:
                raise RuntimeError("ssh-keygen -y returned empty output")
            return pubkey
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            typer.echo(
                f"[colab] failed to derive public key from {identity}: {e}",
                err=True,
            )
            raise typer.Exit(code=2)

    ssh_dir = Path(os.path.expanduser("~/.ssh"))
    for name in _KEY_TYPES:
        candidate = ssh_dir / name
        if candidate.exists():
            return candidate.read_text().strip()
    typer.echo(
        "[colab] no SSH public key found in ~/.ssh/. Run `ssh-keygen -t ed25519` "
        "to generate one, or pass --identity.",
        err=True,
    )
    raise typer.Exit(code=2)


def _resolve_session(name: Optional[str]) -> SessionState:
    """Returns the session to connect to, or exits with an actionable message."""
    from colab_cli.common import state

    resolved = state.resolve_session(name)
    s = state.store.get(resolved)
    if not s:
        typer.echo(
            f"[colab] session '{resolved}' not found. "
            "Run `colab sessions` to list active sessions.",
            err=True,
        )
        raise typer.Exit(code=2)
    return s


def _session_exists(name: str) -> bool:
    """True if a session with this exact name is in the local store."""
    from colab_cli.common import state

    return state.store.get(name) is not None


def _has_local_sessions() -> bool:
    """True if the local store has any session.

    Gates bare ``colab ssh`` auto-create: we only create when there are zero
    sessions, mirroring the gate in ``state.resolve_session`` (which errors on an
    empty store).
    """
    from colab_cli.common import state

    return bool(state.store.list())


def _auto_create_session(
    gpu: Optional[str], tpu: Optional[str], name: Optional[str] = None
) -> SessionState:
    """Creates a runtime via ``colab new`` and returns its session.

    Reuses ``colab new``'s creation path (assignment, keep-alive daemon, scope
    pre-flight) verbatim so the two commands can never drift. ``name`` pins the
    session name; a random one is generated when omitted.
    """
    import uuid

    from colab_cli.commands import session as session_cmd

    name = name or uuid.uuid4().hex[:6]
    typer.echo(f"[colab] Creating runtime '{name}'...")
    session_cmd.new(session=name, gpu=gpu, tpu=tpu)
    return _resolve_session(name)


def _stop_session(name: str) -> None:
    """Best-effort ``colab stop`` for a session (used by ``--rm``)."""
    from colab_cli.commands import session as session_cmd

    try:
        session_cmd.stop(session=name)
    except typer.Exit:
        raise
    except Exception as e:  # cleanup must not mask the shell's own exit
        typer.echo(f"[colab] --rm: failed to stop '{name}': {e}", err=True)


def _build_ws_url(session: SessionState) -> str:
    """Builds the WebSocket URL for the session's SSH endpoint."""
    parsed = urlparse(session.url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return (
        f"{scheme}://{parsed.netloc}{_SSH_PATH}"
        f"?colab-runtime-proxy-token={session.token}"
    )


def _explain_handshake_failure(status: Optional[int], body: bytes) -> str:
    """Maps an upgrade-handshake status/body to an actionable message."""
    snippet = body.decode("utf-8", errors="replace").strip()[:200]
    if status == 400:
        if "missing pubkey" in snippet:
            return (
                "Server rejected request: missing pubkey header. This is likely "
                "a CLI bug; please file a colab-cli issue."
            )
        if "unsupported key type" in snippet:
            return (
                "Server rejected pubkey: unsupported key type. Accepted key "
                "types: ssh-ed25519, ecdsa-sha2-nistp{256,384,521}. RSA keys "
                "(ssh-rsa) are NOT accepted. Generate an Ed25519 key with "
                "`ssh-keygen -t ed25519` and re-run (optionally with --identity)."
            )
        return (
            f"Server rejected pubkey (HTTP 400): {snippet}. "
            "Re-check your key with `ssh-keygen -y -f <key>`."
        )
    if status == 401:
        return (
            "Authentication failed (HTTP 401): the runtime-proxy token is "
            "invalid. The session may have expired - try `colab new`."
        )
    if status == 403:
        return (
            "Forbidden (HTTP 403): the server refused this request; the "
            "runtime-proxy token may lack permission for this action. (A runtime "
            "without SSH enabled returns 404, not 403.)"
        )
    if status == 404:
        return (
            "Endpoint not found (HTTP 404): this runtime does not expose the "
            "/colab/ssh endpoint. SSH is enabled at runtime creation, so an "
            "older or non-SSH runtime will not have it - run `colab new` for a "
            "fresh runtime with SSH."
        )
    if status == 429:
        return (
            "Already-active SSH session (HTTP 429): another `colab ssh` is "
            "connected to this runtime. Disconnect it and retry."
        )
    if status == 502:
        return (
            "Bad gateway (HTTP 502): the runtime's local sshd is unreachable. "
            "The runtime may be unhealthy; try `colab status`, then `colab stop` "
            "+ `colab new`."
        )
    if status is not None:
        return f"WebSocket upgrade rejected (HTTP {status}): {snippet}"
    return (
        f"WebSocket upgrade failed without an HTTP status: {snippet}. "
        "Check your network."
    )


def _connect_websocket(url: str, pubkey: str) -> websocket.WebSocket:
    """Opens the WebSocket, mapping handshake failures to actionable messages."""
    ws = websocket.WebSocket()
    try:
        ws.connect(url, header=[f"{_PUBKEY_HEADER}: {pubkey}"])
        return ws
    except websocket.WebSocketBadStatusException as e:
        status = getattr(e, "status_code", None)
        body = getattr(e, "resp_body", b"") or b""
        if isinstance(body, str):
            body = body.encode("utf-8", errors="replace")
        typer.echo(f"[colab] {_explain_handshake_failure(status, body)}", err=True)
        raise typer.Exit(code=1)
    except (
        websocket.WebSocketAddressException,
        websocket.WebSocketTimeoutException,
        ConnectionRefusedError,
        OSError,
    ) as e:
        typer.echo(
            f"[colab] WebSocket connection failed: {e}. Check your network and "
            "that the runtime is healthy (`colab status`).",
            err=True,
        )
        raise typer.Exit(code=1)


def _bridge_proxy_mode(ws: websocket.WebSocket) -> int:
    """Bridges the WebSocket <-> stdin/stdout for use as an OpenSSH ProxyCommand.

    Returns when either side closes.
    """
    stdin_fd = sys.stdin.buffer.fileno()

    def stdin_to_ws():
        try:
            while True:
                ready, _, _ = select.select([stdin_fd], [], [], None)
                if not ready:
                    continue
                data = os.read(stdin_fd, 8192)
                if not data:
                    break
                ws.send_binary(data)
        except (OSError, websocket.WebSocketException):
            pass
        finally:
            try:
                ws.close()
            except Exception:
                pass

    threading.Thread(target=stdin_to_ws, daemon=True).start()

    try:
        while True:
            opcode, frame = ws.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            if opcode in (websocket.ABNF.OPCODE_BINARY, websocket.ABNF.OPCODE_TEXT):
                if isinstance(frame, str):
                    frame = frame.encode("utf-8")
                sys.stdout.buffer.write(frame)
                sys.stdout.buffer.flush()
    except (websocket.WebSocketException, OSError):
        pass
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return 0


def _shquote(s: str) -> str:
    """Quotes a single argument for an OpenSSH ProxyCommand string."""
    if s and all(c.isalnum() or c in "-_./@:=," for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _proxy_command(session: SessionState, identity: Optional[str]) -> str:
    """Builds the OpenSSH ProxyCommand that bridges this session's WebSocket.

    Used by the interactive shell, which re-invokes this CLI in --proxy-mode. The
    session already exists by then, so only ``-s NAME`` (+ identity) is needed.
    """
    self_cmd = [
        sys.executable,
        "-m",
        "colab_cli.cli",
        "ssh",
        "--proxy-mode",
        "-s",
        session.name,
    ]
    if identity:
        self_cmd.extend(["--identity", identity])
    return " ".join(_shquote(a) for a in self_cmd)


def _ssh_base_args(proxy_command: str, identity: Optional[str]) -> list[str]:
    """Builds the shared ``ssh`` invocation (ProxyCommand + hardening options)."""
    args = [
        "ssh",
        "-o",
        f"ProxyCommand={proxy_command}",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
    ]
    if identity:
        args.extend(["-i", os.path.expanduser(identity)])
    return args


def _run_interactive_ssh(session: SessionState, identity: Optional[str]) -> int:
    """Spawns an interactive ``ssh`` that uses this CLI as its ProxyCommand.

    The subprocess connects to the abstract host ``colab-runtime``; its
    ProxyCommand re-invokes ``colab ssh --proxy-mode`` to bridge the WebSocket.

    A remote command ``cd``s into ``/content`` (Colab's working dir) and then
    execs the login shell, so the user lands where their notebooks/uploads live
    rather than in root's home. ``-t`` forces a PTY (required once a remote
    command is present) so the exec'd shell is interactive; a missing
    ``/content`` is tolerated (stderr suppressed, the shell still starts).
    """
    ssh_args = _ssh_base_args(_proxy_command(session, identity), identity)
    ssh_args.append("-t")
    ssh_args.append(_SSH_HOST)
    ssh_args.append(
        f"cd {_DEFAULT_REMOTE_DIR} 2>/dev/null; exec ${{SHELL:-/bin/bash}} -l"
    )
    return subprocess.call(ssh_args)


def ssh(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    proxy_mode: Annotated[
        bool,
        typer.Option(
            "--proxy-mode",
            help=(
                "Act as an OpenSSH ProxyCommand-compatible WebSocket-stdio "
                "bridge (reads stdin, writes stdout). Use in ~/.ssh/config: "
                "`ProxyCommand colab ssh --proxy-mode -s SESS`. All flags below "
                "also apply here."
            ),
        ),
    ] = False,
    identity: Annotated[
        Optional[str],
        typer.Option(
            "--identity",
            "-i",
            help=(
                "SSH private key whose public key is sent in the "
                "X-Colab-Ssh-Pubkey header (default: first of ~/.ssh/id_ed25519, "
                "id_ecdsa)."
            ),
        ),
    ] = None,
    gpu: Annotated[
        Optional[str],
        typer.Option(
            "--gpu",
            help=(
                "GPU accelerator for a runtime created by this command (T4, L4, "
                "G4, H100, A100). Used when the session is auto-created."
            ),
        ),
    ] = None,
    tpu: Annotated[
        Optional[str],
        typer.Option(
            "--tpu",
            help=(
                "TPU accelerator for a runtime created by this command (v5e1, "
                "v6e1). Used when the session is auto-created."
            ),
        ),
    ] = None,
    rm: Annotated[
        bool,
        typer.Option(
            "--rm",
            help=(
                "Stop the runtime when the session ends. In interactive mode this "
                "applies only to a runtime `colab ssh` auto-created; in "
                "--proxy-mode it stops the bridged session on disconnect "
                "(ephemeral ~/.ssh/config host)."
            ),
        ),
    ] = False,
):
    """Connect to a Colab runtime via SSH.

    Bare ``colab ssh`` uses your only active session, or auto-creates one (like
    ``colab new``) if you have none, and opens a shell in ``/content``. With
    --proxy-mode it is a ProxyCommand-compatible WebSocket-stdio bridge; every
    flag above still applies (``-s NAME`` creates the session if missing,
    ``--gpu/--tpu`` set its accelerator, ``--rm`` stops it on disconnect).
    """
    created = False

    if proxy_mode:
        # --proxy-mode honors every flag. With -s NAME, ensure NAME exists
        # (create with --gpu/--tpu if missing) so a config host works on first
        # connect; creation output is routed to stderr so stdout stays the clean
        # ssh byte stream. --rm stops the session when the bridge closes.
        if session and not _session_exists(session):
            with contextlib.redirect_stdout(sys.stderr):
                s = _auto_create_session(gpu, tpu, name=session)
            created = True
        else:
            s = _resolve_session(session)

        if (gpu or tpu) and not created:
            typer.echo(
                "[colab] --gpu/--tpu ignored: the session already exists.",
                err=True,
            )

        # --rm teardown must survive the way OpenSSH ends a ProxyCommand: on
        # disconnect it sends SIGHUP (verified), not just stdin EOF, and Python's
        # default SIGHUP action terminates us WITHOUT running the `finally` below
        # -- leaking the runtime and its keep-alive daemon. Convert the
        # terminating signals into a clean stop. The `finally` still covers the
        # rare clean-close path; a guard keeps teardown idempotent. Output is
        # routed to stderr because our stdout is the ssh byte stream.
        _rm_state = {"done": False}

        def _do_rm() -> None:
            if rm and not _rm_state["done"]:
                _rm_state["done"] = True
                with contextlib.redirect_stdout(sys.stderr):
                    _stop_session(s.name)

        if rm:
            import signal

            def _on_signal(signum, frame):
                _do_rm()
                os._exit(0)

            for _sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                try:
                    signal.signal(_sig, _on_signal)
                except (ValueError, OSError):
                    pass  # e.g. not running in the main thread

        pubkey = _resolve_pubkey(identity)
        ws = _connect_websocket(_build_ws_url(s), pubkey)
        try:
            code = _bridge_proxy_mode(ws)
        finally:
            _do_rm()
        raise typer.Exit(code=code)

    # Interactive shell.
    if not session and not _has_local_sessions():
        s = _auto_create_session(gpu, tpu)
        created = True
    else:
        s = _resolve_session(session)

    if (gpu or tpu) and not created:
        typer.echo(
            "[colab] --gpu/--tpu ignored: only used when auto-creating a runtime.",
            err=True,
        )

    if rm and not created:
        typer.echo(
            "[colab] --rm ignored: only a runtime auto-created by `colab ssh` "
            "is removed on exit.",
            err=True,
        )

    try:
        code = _run_interactive_ssh(s, identity)
    finally:
        if created and rm:
            _stop_session(s.name)
    raise typer.Exit(code=code)


def register(app: typer.Typer):
    app.command()(ssh)
