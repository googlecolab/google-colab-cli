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
from unittest.mock import MagicMock, patch

import pytest
import requests
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


@patch("colab_cli.contents.requests.Session.request")
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


@patch("colab_cli.contents.requests.Session.request")
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


@patch("colab_cli.contents.requests.Session.request")
def test_404_error(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 404
    mock_request.return_value = mock_resp

    with pytest.raises(FileNotFoundError):
        client.list_dir("nonexistent")


@patch("colab_cli.contents.requests.Session.request")
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


@patch("colab_cli.contents.requests.Session.request")
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


@patch("colab_cli.contents.requests.Session.request")
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


@patch("colab_cli.contents.requests.Session.request")
def test_mkdir(mock_request, client):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 201
    mock_request.return_value = mock_resp

    client.mkdir("content/new_dir")

    mock_request.assert_called_once_with(
        "PUT",
        "https://fake-endpoint.colab.dev/api/contents/content/new_dir",
        params={"authuser": "0", "colab-runtime-proxy-token": "test-token"},
        json={"type": "directory"},
    )


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "root.txt").write_text("root", encoding="utf-8")
    (src / "sub" / "bin.dat").write_bytes(b"\x00\x01\xff")

    count = client.upload_dir(str(src), "content/proj")

    assert count == 2
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/empty",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/root.txt",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub/bin.dat",
    ]
    assert mock_request.call_args_list[0].kwargs["json"] == {"type": "directory"}
    assert mock_request.call_args_list[1].kwargs["json"] == {"type": "directory"}
    assert mock_request.call_args_list[2].kwargs["json"] == {"type": "directory"}
    assert (
        mock_request.call_args_list[4].kwargs["json"]["path"]
        == "content/proj/sub/bin.dat"
    )


@patch("colab_cli.contents.requests.Session.request")
def test_download_directory_raises_with_hint(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"name": "mydir", "type": "directory", "content": []}
    mock_request.return_value = mock_resp

    with pytest.raises(IsADirectoryError, match="-r/--recursive"):
        client.download("content/mydir", str(tmp_path / "mydir"))


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir(mock_request, client, tmp_path):
    responses = {
        "https://fake-endpoint.colab.dev/api/contents/content/proj": {
            "name": "proj",
            "type": "directory",
            "content": [
                {"name": "root.txt", "path": "content/proj/root.txt", "type": "file"},
                {
                    "name": "nb.ipynb",
                    "path": "content/proj/nb.ipynb",
                    "type": "notebook",
                },
                {"name": "sub", "path": "content/proj/sub", "type": "directory"},
                {"name": "empty", "path": "content/proj/empty", "type": "directory"},
            ],
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/root.txt": {
            "name": "root.txt",
            "type": "file",
            "format": "text",
            "content": "hello",
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/nb.ipynb": {
            "name": "nb.ipynb",
            "type": "notebook",
            "format": "json",
            "content": {"cells": [], "nbformat": 4},
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub": {
            "name": "sub",
            "type": "directory",
            "content": [
                {"name": "bin.dat", "path": "content/proj/sub/bin.dat", "type": "file"}
            ],
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/sub/bin.dat": {
            "name": "bin.dat",
            "type": "file",
            "format": "base64",
            "content": base64.b64encode(b"\x00\x02").decode("ascii"),
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/empty": {
            "name": "empty",
            "type": "directory",
            "content": [],
        },
    }

    def side_effect(method, url, **kwargs):
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = responses[url]
        return resp

    mock_request.side_effect = side_effect

    dest = tmp_path / "out"
    count = client.download_dir("content/proj", str(dest))

    assert count == 3
    assert (dest / "root.txt").read_text(encoding="utf-8") == "hello"
    assert json.loads((dest / "nb.ipynb").read_text(encoding="utf-8")) == {
        "cells": [],
        "nbformat": 4,
    }
    assert (dest / "sub" / "bin.dat").read_bytes() == b"\x00\x02"
    assert (dest / "empty").is_dir()


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_single_file(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "solo.txt",
        "type": "file",
        "format": "text",
        "content": "solo",
    }
    mock_request.return_value = mock_resp

    dest = tmp_path / "nested" / "solo.txt"
    count = client.download_dir("content/solo.txt", str(dest))

    assert count == 1
    assert dest.read_text(encoding="utf-8") == "solo"


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_unsafe_entry_name(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": "../escape", "path": "content/escape", "type": "file"}],
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="Unsafe"):
        client.download_dir("content/dir", str(tmp_path / "out"))


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_creates_missing_remote_parents(mock_request, client, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    attempts = {}

    def side_effect(method, url, **kwargs):
        attempts[url] = attempts.get(url, 0) + 1
        resp = MagicMock(spec=Response)
        if url.endswith("/content/a/b/proj") and attempts[url] == 1:
            resp.status_code = 500
            resp.raise_for_status.side_effect = requests.HTTPError("500", response=resp)
            return resp
        if url.endswith("/content/a/b") and attempts[url] == 1:
            resp.status_code = 500
            resp.raise_for_status.side_effect = requests.HTTPError("500", response=resp)
            return resp
        resp.status_code = 200
        return resp

    mock_request.side_effect = side_effect

    count = client.upload_dir(str(src), "content/a/b/proj")

    assert count == 1
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/a/b/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b",
        "https://fake-endpoint.colab.dev/api/contents/content/a",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/a/b/proj/a.txt",
    ]


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_does_not_recurse_on_403(mock_request, client, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    resp = MagicMock(spec=Response)
    resp.status_code = 403
    resp.raise_for_status.side_effect = requests.HTTPError("403", response=resp)
    mock_request.return_value = resp

    with pytest.raises(requests.HTTPError):
        client.upload_dir(str(src), "content/a/proj")

    assert mock_request.call_count == 1


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_does_not_mkdir_content_root_on_500(mock_request, client, tmp_path):
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    resp = MagicMock(spec=Response)
    resp.status_code = 500
    resp.raise_for_status.side_effect = requests.HTTPError("500", response=resp)
    mock_request.return_value = resp

    with pytest.raises(requests.HTTPError):
        client.upload_dir(str(src), "content/proj")

    assert mock_request.call_count == 1


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_skips_symlinked_directories(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    ext = tmp_path / "ext_dir"
    ext.mkdir()
    (ext / "secret.txt").write_text("secret", encoding="utf-8")

    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    (src / "linked_dir").symlink_to(ext, target_is_directory=True)

    count = client.upload_dir(str(src), "content/proj")

    assert count == 1
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/a.txt",
    ]


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_ignores_untrusted_item_path(mock_request, client, tmp_path):
    responses = {
        "https://fake-endpoint.colab.dev/api/contents/content/proj": {
            "name": "proj",
            "type": "directory",
            "content": [
                {
                    "name": "safe.txt",
                    "path": "other/untrusted.txt",
                    "type": "file",
                }
            ],
        },
        "https://fake-endpoint.colab.dev/api/contents/content/proj/safe.txt": {
            "name": "safe.txt",
            "type": "file",
            "format": "text",
            "content": "trusted",
        },
    }

    def side_effect(method, url, **kwargs):
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = responses[url]
        return resp

    mock_request.side_effect = side_effect

    dest = tmp_path / "out"
    count = client.download_dir("content/proj", str(dest))

    assert count == 1
    assert (dest / "safe.txt").read_text(encoding="utf-8") == "trusted"


def test_reuses_http_session_across_requests(client, tmp_path):
    assert isinstance(client._session, requests.Session)
    local_file = tmp_path / "a.txt"
    local_file.write_text("hi", encoding="utf-8")

    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    with patch.object(client._session, "request", return_value=mock_resp) as m_req:
        client.mkdir("content/proj")
        client.upload(str(local_file), "content/proj/a.txt")

    assert m_req.call_count == 2
    assert m_req.call_args_list[0].args[0] == "PUT"
    assert m_req.call_args_list[1].args[0] == "PUT"


@patch("colab_cli.contents.requests.Session.request")
def test_http_error_omits_runtime_proxy_token(mock_request, client):
    resp = Response()
    resp.status_code = 400
    resp.reason = "Bad Request"
    resp.url = (
        "https://fake-endpoint.colab.dev/api/contents/content/x"
        "?authuser=0&colab-runtime-proxy-token=SECRET-JWT"
    )
    mock_request.return_value = resp

    with pytest.raises(requests.HTTPError) as exc_info:
        client.list_dir("content/x")

    assert exc_info.value.response.status_code == 400
    assert exc_info.value.__suppress_context__ is True
    assert exc_info.value.__cause__ is None
    assert "SECRET-JWT" not in str(exc_info.value)
    assert "colab-runtime-proxy-token" not in str(exc_info.value)
    assert "fake-endpoint.colab.dev" not in str(exc_info.value)
    assert "content/x" in str(exc_info.value)


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_skips_symlinked_files_and_broken_symlinks(
    mock_request, client, tmp_path
):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    src = tmp_path / "proj"
    src.mkdir()
    (src / "real.txt").write_text("ok", encoding="utf-8")
    (src / "link.txt").symlink_to(secret)
    (src / "broken.txt").symlink_to(tmp_path / "missing.txt")

    count = client.upload_dir(str(src), "content/proj")

    assert count == 1
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/real.txt",
    ]


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_raises_on_non_directory_or_walk_error(
    mock_request, client, tmp_path, mocker
):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("hi", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        client.upload_dir(str(not_a_dir), "content/proj")
    mock_request.assert_not_called()

    src = tmp_path / "proj"
    src.mkdir()

    def failing_walk(*args, onerror=None, **kwargs):
        onerror(PermissionError("denied"))
        return iter([])

    mocker.patch("colab_cli.contents.os.walk", side_effect=failing_walk)
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    with pytest.raises(PermissionError, match="denied"):
        client.upload_dir(str(src), "content/proj")


@patch("colab_cli.contents.requests.Session.request")
def test_upload_dir_normalizes_remote_dir(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_request.return_value = mock_resp

    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    client.upload_dir(str(src), "./content//proj/")
    urls = [c.args[1] for c in mock_request.call_args_list]
    assert urls == [
        "https://fake-endpoint.colab.dev/api/contents/content/proj",
        "https://fake-endpoint.colab.dev/api/contents/content/proj/a.txt",
    ]

    with pytest.raises(ValueError, match="Unsafe"):
        client.upload_dir(str(src), "../escape")


@pytest.mark.parametrize(
    "bad_name",
    [123, "bad\x00name", "", ".", "..", "a\\b", "a/b"],
)
@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_unsafe_entry_variants(
    mock_request, client, tmp_path, bad_name
):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": bad_name, "type": "file"}],
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="Unsafe"):
        client.download_dir("content/dir", str(tmp_path / "out"))


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_local_symlink(mock_request, client, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "file.txt").symlink_to(outside)

    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": "file.txt", "type": "file"}],
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="symlink"):
        client.download_dir("content/dir", str(dest))

    assert outside.read_text(encoding="utf-8") == "keep"


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_rejects_trailing_slash_local_symlink(
    mock_request, client, tmp_path
):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    dir_resp = MagicMock(spec=Response)
    dir_resp.status_code = 200
    dir_resp.json.return_value = {
        "name": "dir",
        "type": "directory",
        "content": [{"name": "a.txt", "type": "file"}],
    }
    mock_request.return_value = dir_resp
    with pytest.raises(ValueError, match="symlink"):
        client.download_dir("content/dir", f"{link_dir}/")

    file_resp = MagicMock(spec=Response)
    file_resp.status_code = 200
    file_resp.json.return_value = {
        "name": "solo.txt",
        "type": "file",
        "format": "text",
        "content": "nope",
    }
    mock_request.return_value = file_resp
    with pytest.raises(ValueError, match="symlink"):
        client.download_dir("content/solo.txt", f"{link_dir}/")

    assert list(real_dir.iterdir()) == []


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_single_file_into_existing_or_trailing_slash_dir(
    mock_request, client, tmp_path
):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "solo.txt",
        "type": "file",
        "format": "text",
        "content": "solo",
    }
    mock_request.return_value = mock_resp

    existing = tmp_path / "existing"
    existing.mkdir()
    assert client.download_dir("content/solo.txt", str(existing)) == 1
    assert (existing / "solo.txt").read_text(encoding="utf-8") == "solo"

    trailing = tmp_path / "new_dir"
    assert client.download_dir("content/solo.txt", f"{trailing}/") == 1
    assert (trailing / "solo.txt").read_text(encoding="utf-8") == "solo"


@patch("colab_cli.contents.requests.Session.request")
def test_download_dir_max_depth_guard(mock_request, client, tmp_path):
    def side_effect(method, url, **kwargs):
        resp = MagicMock(spec=Response)
        resp.status_code = 200
        resp.json.return_value = {
            "name": "loop",
            "type": "directory",
            "content": [{"name": "loop", "type": "directory"}],
        }
        return resp

    mock_request.side_effect = side_effect

    with pytest.raises(RecursionError, match="Maximum directory depth"):
        client.download_dir("content/loop", str(tmp_path / "out"), _max_depth=3)


@patch("colab_cli.contents.requests.Session.request")
def test_download_none_content_raises(mock_request, client, tmp_path):
    mock_resp = MagicMock(spec=Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "f.txt",
        "type": "file",
        "format": "text",
        "content": None,
    }
    mock_request.return_value = mock_resp

    with pytest.raises(ValueError, match="Missing file content"):
        client.download("content/f.txt", str(tmp_path / "f.txt"))


def test_context_manager_closes_session(session):
    c = ContentsClient(session)
    with patch.object(c._session, "close") as m_close:
        with c as entered:
            assert entered is c
        m_close.assert_called_once_with()
