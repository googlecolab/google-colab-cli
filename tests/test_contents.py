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
import io
import json
from unittest.mock import MagicMock, patch

import pytest
from colab_cli.contents import (
    REQUEST_TIMEOUT,
    UPLOAD_CHUNK_SIZE,
    ContentsClient,
    IncompleteUploadError,
    _upload_payload,
    fits_in_one_request,
)
from requests import HTTPError, Response

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
        json=None,
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
        json=None,
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
        json=None,
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
        json={
            "name": "test.txt",
            "path": "content/test.txt",
            "type": "file",
            "format": "base64",
            "content": expected_b64,
            "chunk": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )


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


class FakeContentsServer:
    """Minimal stand-in for the runtime's Contents API, as observed on Colab.

    PUT follows the Jupyter chunked protocol: chunk 1 replaces the file and
    other chunks are appended. `fail` maps a chunk number to an HTTP status
    (or True for 500) to answer that PUT with; a failed request changes
    nothing, and, like requests, its error text carries the URL and token.
    """

    REASONS = {200: "OK", 403: "Forbidden", 404: "Not Found", 500: "Server Error"}

    def __init__(self, files=None, fail=None):
        self.files = dict(files or {})
        self.fail = fail
        self.calls = []
        self.tokens = []

    def __call__(self, method, url, params=None, json=None, timeout=None):
        assert method == "PUT"
        assert timeout == REQUEST_TIMEOUT
        path = url.split("/api/contents/", 1)[1]
        chunk = json["chunk"]
        self.calls.append((path, chunk))
        self.tokens.append(params["colab-runtime-proxy-token"])
        status = self.fail(chunk) if self.fail else None
        if status:
            status = 500 if status is True else status
            token = params["colab-runtime-proxy-token"]
            return self._response(
                status, error=f"{status} Error for url: {url}?token={token}"
            )
        data = base64.b64decode(json["content"])
        old = b"" if chunk == 1 else self.files.get(path, b"")
        self.files[path] = old + data
        return self._response(200, {"type": "file"})

    @classmethod
    def _response(cls, status, body=None, error=None):
        resp = MagicMock(spec=Response)
        resp.status_code = status
        resp.reason = cls.REASONS[status]
        resp.json.return_value = body
        if error:
            resp.raise_for_status.side_effect = HTTPError(error, response=resp)
        return resp


@pytest.fixture
def small_requests():
    """Shrinks the request limit so that, for content/big.bin, files of up to
    6 bytes fit in one request and larger ones do not. 4-byte chunks fit,
    also as chunk -1, whose envelope is one byte longer than chunk 1's."""
    envelope = len(json.dumps(_upload_payload("content/big.bin", "", 1)))
    with patch("colab_cli.contents.MAX_REQUEST_BODY", envelope + 9):
        yield


def _upload(client, tmp_path, server, content, chunk_size=4):
    local_file = tmp_path / "big.bin"
    local_file.write_bytes(content)
    with patch("colab_cli.contents.requests.request", side_effect=server):
        client.upload(str(local_file), "content/big.bin", chunk_size=chunk_size)


def test_upload_large_file_in_chunks(client, tmp_path, small_requests):
    """Files that do not fit in one request are sent as chunks 1, 2, ..., -1."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})

    _upload(client, tmp_path, server, b"0123456789")

    assert server.files == {"content/big.bin": b"0123456789"}
    assert server.calls == [
        ("content/big.bin", 1),
        ("content/big.bin", 2),
        ("content/big.bin", -1),
    ]


def test_upload_exact_multiple_of_chunk_size(client, tmp_path, small_requests):
    """The last full chunk is marked -1; no empty trailing request is sent."""
    server = FakeContentsServer()

    _upload(client, tmp_path, server, b"01234567")

    assert server.files == {"content/big.bin": b"01234567"}
    assert server.calls == [("content/big.bin", 1), ("content/big.bin", -1)]


@pytest.mark.parametrize("content", [b"", b"0123", b"012345"])
def test_upload_that_fits_is_one_request(client, tmp_path, small_requests, content):
    """A file that fits in one request is sent as a single chunk=1 request,
    even when it is larger than one chunk. A lone chunk=-1 would append to
    the old file instead of replacing it."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})

    _upload(client, tmp_path, server, content)

    assert server.files == {"content/big.bin": content}
    assert server.calls == [("content/big.bin", 1)]


def test_one_request_limit_matches_colab():
    """Pinned to a live Colab measurement for this path: a 78,643,113-byte file
    (a 104,857,600-byte request body) uploaded in one request, and larger
    files were refused with HTTP 400."""
    assert fits_in_one_request("/content/p_edge.bin", 78_643_113)
    assert not fits_in_one_request("/content/p_edge.bin", 78_643_114)


def test_default_chunk_fits_in_one_request():
    long_path = "content/" + "x" * 1000
    assert fits_in_one_request(long_path, UPLOAD_CHUNK_SIZE)


@pytest.mark.parametrize("status, error", [(404, FileNotFoundError), (403, HTTPError)])
def test_rejected_first_chunk_raises_original_error(
    client, tmp_path, small_requests, status, error
):
    """A runtime that refuses chunk 1 wrote nothing, so the plain error is kept."""
    server = FakeContentsServer(
        {"content/big.bin": b"old contents"},
        fail=lambda chunk: status if chunk == 1 else None,
    )

    with pytest.raises(error):
        _upload(client, tmp_path, server, b"0123456789")

    assert server.files == {"content/big.bin": b"old contents"}


def test_failed_single_request_raises_original_error(client, tmp_path):
    """A one-request upload either replaces the file or leaves it as it was."""
    server = FakeContentsServer(
        {"content/big.bin": b"old contents"}, fail=lambda chunk: True
    )

    with pytest.raises(HTTPError):
        _upload(client, tmp_path, server, b"0123")

    assert server.files == {"content/big.bin": b"old contents"}


@pytest.mark.parametrize("failed_chunk", [1, 2])
def test_failed_chunk_reports_possibly_incomplete_file(
    client, tmp_path, small_requests, failed_chunk
):
    """Any failed chunk may have been stored (e.g. the response was lost), so
    the error says the file may be incomplete. The HTTPError text has the
    token, so it must reach neither the message nor a chained traceback."""
    server = FakeContentsServer(fail=lambda chunk: chunk == failed_chunk)

    with pytest.raises(IncompleteUploadError) as excinfo:
        _upload(client, tmp_path, server, b"0123456789")

    message = str(excinfo.value)
    assert "content/big.bin" in message
    sent = failed_chunk - 1
    assert f"after {sent} of its chunks were sent (HTTPError 500 Server Error)" in (
        message
    )
    assert "may be incomplete" in message
    assert "test-token" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert len(server.calls) == failed_chunk


def test_later_chunks_use_a_refreshed_token(session, tmp_path, small_requests):
    """Long uploads can outlast the proxy token, so every chunk after the first
    asks the session for a possibly renewed one."""
    renewed = session.model_copy(update={"token": "renewed-token"})
    client = ContentsClient(session, refresh_session=lambda: renewed)
    server = FakeContentsServer()

    _upload(client, tmp_path, server, b"0123456789")

    assert server.tokens == ["test-token", "renewed-token", "renewed-token"]


def test_failed_token_refresh_reports_possibly_incomplete_file(
    session, tmp_path, small_requests
):
    """Refreshing can end in sys.exit (e.g. failed ADC credentials) after chunk 1
    replaced the remote file, so it is reported like a failed chunk."""

    def refresh_session():
        raise SystemExit(1)

    client = ContentsClient(session, refresh_session=refresh_session)
    server = FakeContentsServer()

    with pytest.raises(
        IncompleteUploadError, match=r"after 1 of its chunks.*SystemExit"
    ):
        _upload(client, tmp_path, server, b"0123456789")

    assert server.calls == [("content/big.bin", 1)]


@pytest.mark.parametrize("content", [b"", b"0123"])
def test_file_that_shrank_after_its_size_was_read(
    client, tmp_path, small_requests, content
):
    """If the file is truncated between the size check and the first read, the
    rest is sent as one chunk=1 request instead of failing."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})

    with patch("colab_cli.contents.os.fstat", return_value=MagicMock(st_size=10**9)):
        _upload(client, tmp_path, server, content)

    assert server.files == {"content/big.bin": content}
    assert server.calls == [("content/big.bin", 1)]


class FailingReader(io.BytesIO):
    """A local file whose reads fail after the first `ok_reads`."""

    def __init__(self, data, ok_reads):
        super().__init__(data)
        self.ok_reads = ok_reads

    def fileno(self):
        return -1

    def read(self, size=-1):
        if self.ok_reads == 0:
            raise OSError(5, "Input/output error")
        self.ok_reads -= 1
        return super().read(size)


def test_failed_local_read_reports_possibly_incomplete_file(client, small_requests):
    """Chunk 1 already replaced the remote file, so a local read error later
    on must also say the remote file may be incomplete."""
    server = FakeContentsServer({"content/big.bin": b"old contents"})
    reader = FailingReader(b"0123456789", ok_reads=2)

    with (
        patch("colab_cli.contents.open", create=True, return_value=reader),
        patch("colab_cli.contents.os.fstat", return_value=MagicMock(st_size=10)),
        patch("colab_cli.contents.requests.request", side_effect=server),
        pytest.raises(IncompleteUploadError) as excinfo,
    ):
        client.upload("big.bin", "content/big.bin", chunk_size=4)

    assert "Input/output error" in str(excinfo.value)
    assert "may be incomplete" in str(excinfo.value)
    assert server.files == {"content/big.bin": b"0123"}


@pytest.mark.parametrize("chunk_size", [0, -1, 7])
def test_upload_rejects_bad_chunk_size(client, tmp_path, small_requests, chunk_size):
    """Chunks must be positive and fit in one request (7 bytes do not here)."""
    with pytest.raises(ValueError):
        _upload(client, tmp_path, FakeContentsServer(), b"0123", chunk_size)
