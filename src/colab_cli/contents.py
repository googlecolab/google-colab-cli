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
import os
from typing import Callable, Optional
from urllib.parse import quote

import requests

from colab_cli.state import SessionState
from colab_cli.utils import get_status_code

# (connect, read) seconds. The read timeout bounds the wait between bytes, not
# the whole transfer, so large uploads/downloads still finish; a dropped
# connection fails instead of hanging forever.
REQUEST_TIMEOUT = (30, 300)

# Colab runtimes reject request bodies over 100 MiB with HTTP 400. Files are
# sent base64-encoded (4/3 the size) inside JSON, so a file that does not fit
# in one request goes up in chunks of UPLOAD_CHUNK_SIZE (about 85 MiB encoded).
MAX_REQUEST_BODY = 100 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024


class IncompleteUploadError(IOError):
    """A chunked upload failed partway, so the remote file may hold only part of
    the local file."""


def describe_error(e: Exception) -> str:
    """Describes an error without the request URL, which has the proxy token."""
    if isinstance(e, requests.RequestException):
        status = get_status_code(e)
        reason = getattr(getattr(e, "response", None), "reason", None)
        details = [str(part) for part in (status, reason) if part]
        return " ".join([type(e).__name__, *details])
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _upload_payload(remote_path: str, content: str, chunk: int) -> dict:
    return {
        "name": remote_path.split("/")[-1],
        "path": remote_path,
        "type": "file",
        "format": "base64",
        "content": content,
        "chunk": chunk,
    }


def fits_in_one_request(remote_path: str, size: int, chunk: int = 1) -> bool:
    """Whether `size` bytes can be uploaded to remote_path as `chunk` in one
    request, i.e. whether its JSON body stays within MAX_REQUEST_BODY."""
    envelope = len(json.dumps(_upload_payload(remote_path, "", chunk)))
    return envelope + 4 * -(-size // 3) <= MAX_REQUEST_BODY


class ContentsClient:
    def __init__(
        self,
        session_state: SessionState,
        refresh_session: Optional[Callable[[], SessionState]] = None,
    ):
        """refresh_session, if given, is called before every chunk after the
        first to pick up a renewed runtime proxy token, so that uploads that
        outlast the token do not fail partway."""
        self.base_url = session_state.url.rstrip("/")
        self.token = session_state.token
        self._refresh_session = refresh_session

    def _request(
        self, method: str, path: str, params: dict = None, json_data: dict = None
    ):
        # Quote the path, but don't encode slashes so directory paths stay intact
        quoted_path = quote(path.strip("/"), safe="/")
        url = f"{self.base_url}/api/contents/{quoted_path}"

        req_params = {"authuser": "0", "colab-runtime-proxy-token": self.token}
        if params:
            req_params.update(params)

        response = requests.request(
            method, url, params=req_params, json=json_data, timeout=REQUEST_TIMEOUT
        )

        if get_status_code(response) == 404:
            raise FileNotFoundError(f"File or directory not found: {path}")

        response.raise_for_status()

        # DELETE doesn't return JSON
        if method == "DELETE":
            return None

        return response.json()

    def list_dir(self, path: str):
        return self._request("GET", path)

    def upload(
        self, local_path: str, remote_path: str, chunk_size: int = UPLOAD_CHUNK_SIZE
    ):
        """Uploads a file, in chunks if it does not fit in one request.

        A file that fits is sent as before: one request with chunk 1, which
        replaces the remote file in a single save, so the file is either
        replaced completely or left as it was. A larger file uses the Jupyter
        Contents API chunked protocol: chunk 1 replaces the file, later chunks
        are appended, and -1 marks the last one. A lone -1 would append to an
        existing file, so it is only ever sent after chunk 1.

        If a chunk, or refresh_session before it, fails, IncompleteUploadError
        is raised: the remote file may be partly written (even a failed chunk 1
        may have been stored if only the response was lost) and the upload has
        to be run again. A chunk 1 the runtime refused (404 or another 4xx)
        wrote nothing, so that error is raised unchanged. IncompleteUploadError
        is raised outside the `except` block so that it carries no reference
        to the requests error, whose text includes the proxy token. An upload
        interrupted with Ctrl-C after chunk 1 also leaves a partial file.

        The stored size is not compared with the local one: the Contents API
        reports a symlink's own size (lstat), so an upload that wrote through
        a symlinked destination would look incomplete.
        """
        if chunk_size <= 0 or not fits_in_one_request(remote_path, chunk_size, -1):
            raise ValueError(f"chunk_size {chunk_size} does not fit in one request")
        with open(local_path, "rb") as f:
            if fits_in_one_request(remote_path, os.fstat(f.fileno()).st_size):
                return self._put_chunk(remote_path, f.read(), 1)
            data = f.read(chunk_size)
            next_data = f.read(chunk_size)
            if not next_data:
                return self._put_chunk(remote_path, data, 1)
            sent = 0
            failure = None
            try:
                while data:
                    if sent:
                        self._refresh_token()
                    model = self._put_chunk(
                        remote_path, data, sent + 1 if next_data else -1
                    )
                    sent += 1
                    data, next_data = next_data, f.read(chunk_size)
            except (Exception, SystemExit) as e:
                status = get_status_code(e)
                rejected = isinstance(e, FileNotFoundError) or (
                    status is not None and 400 <= status < 500
                )
                if not sent and rejected:
                    raise
                failure = (
                    f"Uploading {remote_path} failed after {sent} of its chunks "
                    f"were sent ({describe_error(e)}); "
                    "the remote file may be incomplete."
                )
        if failure:
            raise IncompleteUploadError(failure)
        return model

    def _refresh_token(self):
        if self._refresh_session:
            session_state = self._refresh_session()
            self.base_url = session_state.url.rstrip("/")
            self.token = session_state.token

    def _put_chunk(self, remote_path: str, data: bytes, chunk: int):
        content = base64.b64encode(data).decode("ascii")
        payload = _upload_payload(remote_path, content, chunk)
        return self._request("PUT", remote_path, json_data=payload)

    def download(self, remote_path: str, local_path: str):
        data = self._request("GET", remote_path, params={"content": "1"})

        if data.get("type") == "directory":
            raise IsADirectoryError(f"Cannot download a directory: {remote_path}")

        content = data.get("content", "")
        fmt = data.get("format")

        if fmt == "base64":
            content_bytes = base64.b64decode(content)
        elif fmt == "json":
            content_bytes = json.dumps(content, indent=2).encode("utf-8")
        else:
            # Assume text if it's not base64 explicitly encoded
            content_bytes = str(content).encode("utf-8")

        with open(local_path, "wb") as f:
            f.write(content_bytes)

    def rm(self, remote_path: str):
        self._request("DELETE", remote_path)
