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

"""Real-wire contract tests for `colab ssh`.

Why this file exists: the mocked suite in tests/test_ssh.py mocks the websocket
at the boundary (`_connect_websocket`, `websocket.WebSocket.connect`, or
`_bridge_proxy_mode`), so the one line that actually puts bytes on the wire --

    ws.connect(url, header=[f"{_PUBKEY_HEADER}: {pubkey}"])   # ssh.py

-- never runs under test. A wrong `_SSH_PATH` (e.g. the stale `/api/colab/ssh`)
or a wrong `_PUBKEY_HEADER` would pass every mocked test while breaking every
real connection. These tests close that gap: they stand up a real loopback
WebSocket server, drive the client's real connect path (`_build_ws_url` +
`_connect_websocket`, no mocks), and assert on the bytes the server actually
received -- the request path, the runtime-proxy-token query param, and the
pubkey header name+value. The server contract mirrors the google3 backend
(third_party/colab/sources/{server.ts,websocket_to_ssh.ts}): route
`/colab/ssh`, header `x-colab-ssh-pubkey`, 400 `unsupported key type`, 429
already-active-session.

Fully offline (~0.15s); allocates no Colab runtime.
"""

import base64
import hashlib
import socket
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from colab_cli.commands import ssh
from colab_cli.state import SessionState
import pytest
import typer

# A structurally-valid ed25519 public key. ed25519 is the only key type the
# server accepts (RSA .pub tokens are `ssh-rsa`, rejected with 400 -- see
# websocket_to_ssh.ts ALLOWED_KEY_TYPES). The distinctive marker lets us prove
# the value crosses the wire verbatim.
PUBKEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIWIRECONTRACTMARKER00000000000000000000 "
    "wire-contract@colab-cli-test"
)
TOKEN = "WIRE_CONTRACT_TOKEN_abc123"

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --- loopback capture server -------------------------------------------------


@dataclass
class CapturedRequest:
    """The raw HTTP upgrade request the client sent, parsed."""

    raw: bytes
    method: str
    target: str  # path + query, e.g. "/colab/ssh?colab-runtime-proxy-token=..."
    headers: Dict[str, str] = field(default_factory=dict)
    headers_lower: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    @property
    def query(self) -> str:
        return self.target.split("?", 1)[1] if "?" in self.target else ""


def _parse_request(raw: bytes) -> CapturedRequest:
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    method, target, _proto = lines[0].split(" ", 2)
    req = CapturedRequest(raw=raw, method=method, target=target)
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        req.headers[name] = value
        req.headers_lower[name.lower()] = (name, value)
    return req


class LoopbackWSServer:
    """A single-shot loopback server that captures the client's upgrade request.

    Two modes:
      * mode="handshake": complete a minimal RFC6455 101 handshake so the real
        `_connect_websocket` returns a live WebSocket (success path).
      * mode="status":    return a controlled HTTP status + body (with
        Content-Length so websocket-client populates resp_body), exercising the
        real WebSocketBadStatusException error-mapping path.
    """

    def __init__(
        self,
        mode: str = "handshake",
        status: int = 400,
        reason: str = "Bad Request",
        body: bytes = b"",
    ):
        self.mode = mode
        self.status = status
        self.reason = reason
        self.body = body
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.captured: Optional[CapturedRequest] = None
        self._captured_evt = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "LoopbackWSServer":
        self._thread.start()
        return self

    def _serve(self) -> None:
        self._sock.settimeout(10)
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            if data:
                self.captured = _parse_request(data)
                self._captured_evt.set()
            try:
                conn.sendall(self._response(self.captured))
            except OSError:
                return
            if self.mode == "handshake":
                # Drain the client's close frame so its ws.close() returns
                # promptly, then let the socket close.
                try:
                    conn.recv(4096)
                except OSError:
                    pass

    def _response(self, req: Optional[CapturedRequest]) -> bytes:
        if self.mode == "handshake":
            key = (
                req.headers_lower.get("sec-websocket-key", ("", ""))[1]
                if req
                else ""
            )
            accept = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode()).digest()
            ).decode()
            return (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode()
        # status mode: MUST send Content-Length -- websocket-client only reads
        # the response body into WebSocketBadStatusException.resp_body when
        # Content-Length is present. Omitting it would silently degrade the
        # client to the generic 400 message and hide the `unsupported key type`
        # branch.
        headers = (
            f"HTTP/1.1 {self.status} {self.reason}\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(self.body)}\r\n"
            "\r\n"
        ).encode()
        return headers + self.body

    def wait_for_request(self, timeout: float = 10.0) -> CapturedRequest:
        if not self._captured_evt.wait(timeout):
            raise AssertionError("loopback server never captured a request")
        assert self.captured is not None
        return self.captured

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


@pytest.fixture
def ws_server_factory():
    servers: List[LoopbackWSServer] = []

    def _make(**kw) -> LoopbackWSServer:
        s = LoopbackWSServer(**kw).start()
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


def _session_for(port: int, token: str = TOKEN) -> SessionState:
    """A real SessionState (not a MagicMock) pointing at the loopback server."""
    return SessionState(
        name="wire-test",
        token=token,
        url=f"http://127.0.0.1:{port}",
        endpoint="wire-endpoint",
    )


# --- the reusable contract assertion (this is what mutation must break) ------


def _assert_wire_contract(
    req: CapturedRequest, token: str, pubkey: str
) -> None:
    """Assert the captured upgrade request honors the SSH wire contract.

    A wrong `_SSH_PATH` breaks the path/query assertions; a wrong
    `_PUBKEY_HEADER` breaks the header-name assertion. The mocked suite can
    catch neither -- see test_mutation_* below, which run the SAME assertion
    against a mutated client and prove it raises.
    """
    assert req.method == "GET", f"expected GET, got {req.method!r}"
    assert req.path == "/colab/ssh", f"wrong route path: {req.path!r}"
    assert f"colab-runtime-proxy-token={token}" in req.query, (
        f"token missing/wrong in query: {req.query!r}"
    )
    assert "x-colab-ssh-pubkey" in req.headers_lower, (
        f"pubkey header absent; headers sent: {sorted(req.headers)!r}"
    )
    sent_name, sent_value = req.headers_lower["x-colab-ssh-pubkey"]
    assert sent_name == "X-Colab-Ssh-Pubkey", (
        f"header name on wire: {sent_name!r}"
    )
    assert sent_value == pubkey, f"pubkey not verbatim: {sent_value!r}"


# --- the real-wire contract, success path ------------------------------------


def test_wire_contract_path_token_and_pubkey_header(ws_server_factory):
    """The real connect path puts the contracted bytes on the wire.

    No mock of _connect_websocket / websocket.connect: the header-emitting line
    in ssh.py executes for real against a loopback server, and we assert on what
    the server received.
    """
    server = ws_server_factory(mode="handshake")
    session = _session_for(server.port)

    url = ssh._build_ws_url(session)
    ws = ssh._connect_websocket(url, PUBKEY)  # real handshake, real header line
    try:
        ws.close()
    except Exception:
        pass

    req = server.wait_for_request()
    _assert_wire_contract(req, TOKEN, PUBKEY)


def test_wire_contract_distinct_token_reaches_wire(ws_server_factory):
    """A per-session token is what actually appears in the query string."""
    server = ws_server_factory(mode="handshake")
    token = "DISTINCT_TOKEN_zzz999"
    session = _session_for(server.port, token=token)

    ws = ssh._connect_websocket(ssh._build_ws_url(session), PUBKEY)
    try:
        ws.close()
    except Exception:
        pass

    req = server.wait_for_request()
    assert f"colab-runtime-proxy-token={token}" in req.query


# --- prove the contract catches the mutations the mocked suite misses ---------


@pytest.mark.parametrize(
    ("attr", "value", "reached_wire"),
    [
        (
            "_SSH_PATH",
            "/api/colab/ssh",
            lambda req: req.path == "/api/colab/ssh",
        ),
        (
            "_PUBKEY_HEADER",
            "X-Wrong-Ssh-Pubkey",
            lambda req: (
                "x-wrong-ssh-pubkey" in req.headers_lower
                and "x-colab-ssh-pubkey" not in req.headers_lower
            ),
        ),
    ],
    ids=["wrong-ssh-path", "wrong-pubkey-header"],
)
def test_mutation_reaches_wire_and_breaks_contract(
    ws_server_factory, monkeypatch, attr, value, reached_wire
):
    """Mutating a wire constant (path / header name) really changes the bytes
    on the wire and makes the contract assertion fail -- something the mocked
    suite cannot detect."""
    monkeypatch.setattr(ssh, attr, value)
    server = ws_server_factory(mode="handshake")
    session = _session_for(server.port)

    ws = ssh._connect_websocket(ssh._build_ws_url(session), PUBKEY)
    try:
        ws.close()
    except Exception:
        pass

    req = server.wait_for_request()
    assert reached_wire(req)  # the mutation really reached the wire
    with pytest.raises(AssertionError):
        _assert_wire_contract(req, TOKEN, PUBKEY)


# --- real HTTP status mapping (no mocked exception) --------------------------


@pytest.mark.parametrize(
    ("status", "reason", "body", "must_contain"),
    [
        (400, "Bad Request", b"unsupported key type", "unsupported key type"),
        (
            429,
            "Too Many Requests",
            b'{"error":"already-active-session"}',
            "Already-active SSH",
        ),
    ],
)
def test_real_status_mapping_via_loopback(
    ws_server_factory, capsys, status, reason, body, must_contain
):
    """A real HTTP error from the server -> real WebSocketBadStatusException ->
    `_explain_handshake_failure` -> actionable stderr + exit code 1.

    Exercises the genuine error path (including websocket-client reading the
    Content-Length body into resp_body), which the mocked suite only reaches by
    hand-constructing the exception with a mocked resp_body.
    """
    server = ws_server_factory(
        mode="status", status=status, reason=reason, body=body
    )
    session = _session_for(server.port)

    with pytest.raises(typer.Exit) as exc_info:
        ssh._connect_websocket(ssh._build_ws_url(session), PUBKEY)
    assert exc_info.value.exit_code == 1

    err = capsys.readouterr().err
    assert must_contain in err
    if status == 400:
        # the remediation hint only fires when resp_body was really read
        assert "ssh-keygen -t ed25519" in err

    # the server must still have seen a well-formed contracted request even on
    # the rejection path.
    req = server.wait_for_request()
    _assert_wire_contract(req, TOKEN, PUBKEY)
