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

Three modes:

* ``colab ssh``                    pick the only active session and open an
                                   interactive shell.
* ``colab ssh -s SESSION``         same, targeting SESSION explicitly.
* ``colab ssh --proxy-mode -s S``  act as an OpenSSH ProxyCommand-compatible
                                   WebSocket-stdio bridge, so::

                                     Host colab-runtime
                                       ProxyCommand colab ssh --proxy-mode -s S

                                   in ``~/.ssh/config`` works with any SSH-based
                                   IDE / remote-development tool.

``--identity/-i`` overrides the default key order (``~/.ssh/id_ed25519`` ->
``id_ecdsa`` -> ``id_rsa``); the public key is derived via ``ssh-keygen -y -f``
and sent in the ``X-Colab-Ssh-Pubkey`` header.
"""

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
_KEY_TYPES = ["id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"]
_PUBKEY_HEADER = "X-Colab-Ssh-Pubkey"


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
                "Server rejected pubkey: unsupported key type. Accepted: "
                "ssh-ed25519, ecdsa-sha2-nistp{256,384,521}, rsa-sha2-{256,512}. "
                "Bare ssh-rsa (SHA-1) is not accepted. Try `ssh-keygen -t ed25519` "
                "and re-run with --identity."
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
            "Forbidden (HTTP 403): the token is valid but the feature is not "
            "enabled for this session. Verify SSH is enabled in your Colab tier."
        )
    if status == 404:
        return (
            "Endpoint not found (HTTP 404): the runtime does not expose the SSH "
            "endpoint. It may need to be started with SSH access enabled."
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


def _run_interactive_ssh(session: SessionState, identity: Optional[str]) -> int:
    """Spawns an interactive ``ssh`` that uses this CLI as its ProxyCommand.

    The subprocess connects to the abstract host ``colab-runtime``; its
    ProxyCommand re-invokes ``colab ssh --proxy-mode`` to bridge the WebSocket.
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
    proxy_command = " ".join(_shquote(a) for a in self_cmd)

    ssh_args = [
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
        ssh_args.extend(["-i", os.path.expanduser(identity)])
    ssh_args.append("root@colab-runtime")

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
                "bridge (reads stdin, writes stdout). Use as: "
                '`ssh -o ProxyCommand="colab ssh --proxy-mode -s SESS" host`.'
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
                "id_ecdsa, id_rsa)."
            ),
        ),
    ] = None,
):
    """Connect to a Colab runtime via SSH.

    Without --proxy-mode opens an interactive shell; with --proxy-mode runs as a
    ProxyCommand-compatible WebSocket-stdio bridge.
    """
    s = _resolve_session(session)
    pubkey = _resolve_pubkey(identity)
    url = _build_ws_url(s)

    if proxy_mode:
        ws = _connect_websocket(url, pubkey)
        raise typer.Exit(code=_bridge_proxy_mode(ws))

    raise typer.Exit(code=_run_interactive_ssh(s, identity))


def register(app: typer.Typer):
    app.command()(ssh)
