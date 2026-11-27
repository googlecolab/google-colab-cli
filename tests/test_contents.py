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

import base64
import json
from unittest.mock import ANY, MagicMock, patch

import pytest
from colab_cli.contents import REQUEST_TIMEOUT, ContentsClient
from requests import Response

from colab_cli.state import SessionState


@pytest.fixture
def session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://fake-endpoint.colab.dev",
        endpoint="endpoint",
    )


@pytest.fixture
def client(session):
    return ContentsClient(session)


@patch("colab_cli.contents.requests.request")
def test_list_dir(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "content",
        "type": "directory",
        "content": [
            {"name": "file.txt", "type": "file"},
            {"name": "dir", "type": "directory"},
        ],
    }
    mock_request.return_value = mock_resp

    res = client.list_dir("content")

    mock_request.assert_called_once_with(
        "GET",
        "https://fake-endpoint.colab.dev/api/contents/content",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        data=None,
        headers=None,
        timeout=REQUEST_TIMEOUT,
    )
    assert res["type"] == "directory"
    assert len(res["content"]) == 2


@patch("colab_cli.contents.requests.request")
def test_rm_file(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 204
    mock_request.return_value = mock_resp

    client.rm("content/file.txt")

    mock_request.assert_called_once_with(
        "DELETE",
        "https://fake-endpoint.colab.dev/api/contents/content/file.txt",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        data=None,
        headers=None,
        timeout=REQUEST_TIMEOUT,
    )


@patch("colab_cli.contents.requests.request")
def test_404_error(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 404
    mock_request.return_value = mock_resp

    with pytest.raises(FileNotFoundError):
        client.list_dir("nonexistent")


@patch("colab_cli.contents.requests.request")
def test_download_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200

    # Mocking a base64 encoded response
    content_bytes = b"Hello world!"
    b64_content = base64.b64encode(content_bytes).decode("ascii")

    mock_resp.json.return_value = {
        "name": "test.txt",
        "type": "file",
        "format": "base64",
        "content": b64_content,
    }
    mock_request.return_value = mock_resp

    local_file = tmp_path / "test.txt"
    client.download("content/test.txt", str(local_file))

    mock_request.assert_called_once_with(
        "GET",
        "https://fake-endpoint.colab.dev/api/contents/content/test.txt",
        params={
            "authuser": "0",
            "colab-runtime-proxy-token": "test-token",
            "content": "1",
        },
        data=None,
        headers=None,
        timeout=REQUEST_TIMEOUT,
    )

    assert local_file.read_bytes() == content_bytes


@patch("colab_cli.contents.requests.request")
def test_download_notebook_json_format(mock_request, client, tmp_path):
    """Downloading a format=json payload serializes valid 2-space indented JSON."""
    notebook_doc = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Demo"]},
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "outputs": [],
                "source": ["x = 42\n", "print(x)"],
            },
        ],
        "metadata": {"kernelspec": {"display_name": "Python 3", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "demo.ipynb",
        "type": "notebook",
        "format": "json",
        "content": notebook_doc,
    }
    mock_request.return_value = mock_resp

    local_file = tmp_path / "demo.ipynb"
    client.download("content/demo.ipynb", str(local_file))

    raw = local_file.read_text(encoding="utf-8")
    assert json.loads(raw) == notebook_doc
    assert raw == json.dumps(notebook_doc, indent=2)


@patch("colab_cli.contents.requests.request")
def test_upload_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    local_file = tmp_path / "test.txt"
    content_bytes = b"Hello upload!"
    local_file.write_bytes(content_bytes)

    client.upload(str(local_file), "content/test.txt")

    expected_b64 = base64.b64encode(content_bytes).decode("ascii")

    mock_request.assert_called_once_with(
        "PUT",
        "https://fake-endpoint.colab.dev/api/contents/content/test.txt",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        data=ANY,
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    assert json.loads(mock_request.call_args.kwargs["data"].read()) == {
        "name": "test.txt",
        "path": "content/test.txt",
        "type": "file",
        "format": "base64",
        "content": expected_b64,
        "chunk": 1,
    }


def test_request_times_out_when_server_stops_responding(client, monkeypatch):
    # A server that accepts the connection but never answers (e.g. the network
    # dropped mid-transfer) must not make upload/download hang forever.
    import socket
    import threading

    import requests

    import colab_cli.contents as contents

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client.base_url = f"http://127.0.0.1:{server.getsockname()[1]}"
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(contents, "REQUEST_TIMEOUT", (0.01, 0.01))

    errors = []

    def call():
        try:
            client.list_dir("content")
        except Exception as e:
            errors.append(e)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(1)
    server.close()

    assert not worker.is_alive(), "request hung with no timeout"
    assert isinstance(errors[0], requests.exceptions.Timeout)


def test_upload_sends_the_body_in_small_writes(client, tmp_path, monkeypatch):
    """urllib3 applies the connect timeout to the socket writes of the request
    body, so a body written in one call fails whenever it takes longer than that
    to send, even while it keeps moving. It must go out in small writes, each
    with its own timeout, and still arrive as the same JSON request."""
    import socket
    import threading

    import colab_cli.contents as contents

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server_address = server.getsockname()
    client.base_url = f"http://127.0.0.1:{server_address[1]}"
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(contents, "REQUEST_TIMEOUT", (1, 1))
    received = []

    def respond():
        conn, _ = server.accept()
        with conn:
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
                head, sep, body = data.partition(b"\r\n\r\n")
                if not sep:
                    continue
                headers = dict(
                    line.partition(b": ")[::2] for line in head.lower().split(b"\r\n")
                )
                if len(body) >= int(headers.get(b"content-length", 0)):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    received.append((headers, body))
                    return

    write_sizes = []
    real_sendall = socket.socket.sendall

    def sendall(sock, data, *args):
        try:
            to_server = sock.getpeername() == server_address
        except OSError:
            to_server = False
        if to_server:
            write_sizes.append(len(data))
        return real_sendall(sock, data, *args)

    monkeypatch.setattr(socket.socket, "sendall", sendall)
    server_thread = threading.Thread(target=respond, daemon=True)
    server_thread.start()
    content = bytes(range(256)) * 4096
    local_file = tmp_path / "big.bin"
    local_file.write_bytes(content)

    client.upload(str(local_file), "content/big.bin")
    server_thread.join(1)
    server.close()

    assert not server_thread.is_alive()
    assert received, "the server did not receive a complete request"
    headers, body = received[0]
    assert headers.get(b"content-length") == str(len(body)).encode()
    assert headers[b"content-type"] == b"application/json"
    assert json.loads(body) == {
        "name": "big.bin",
        "path": "content/big.bin",
        "type": "file",
        "format": "base64",
        "content": base64.b64encode(content).decode("ascii"),
        "chunk": 1,
    }
    assert len(write_sizes) > 1
    assert max(write_sizes) <= 64 * 1024
