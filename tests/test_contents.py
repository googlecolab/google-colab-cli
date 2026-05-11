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
from unittest.mock import MagicMock, patch

import pytest
from colab_cli.contents import ContentsClient
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
        json=None,
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
    )

    assert local_file.read_bytes() == content_bytes


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
    )
