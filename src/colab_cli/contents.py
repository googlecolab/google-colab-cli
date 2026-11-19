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
import ntpath
import os
import posixpath
from urllib.parse import quote

import requests

from colab_cli.state import SessionState
from colab_cli.utils import get_status_code


def _write_file_content(data: dict, local_path: str) -> None:
    content = data.get("content")
    if content is None:
        raise ValueError("Missing file content in server response")
    fmt = data.get("format")

    if fmt == "base64":
        content_bytes = base64.b64decode(content)
    elif fmt == "json":
        content_bytes = json.dumps(content, indent=2).encode("utf-8")
    else:
        content_bytes = str(content).encode("utf-8")

    with open(local_path, "wb") as f:
        f.write(content_bytes)


def _normalize_remote_path(remote_path: str) -> str:
    cleaned = remote_path.strip("/")
    if not cleaned:
        return ""
    normalized = posixpath.normpath(cleaned)
    if normalized == ".":
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"Unsafe remote path: {remote_path!r}")
    return normalized


def _validate_entry_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or ntpath.splitdrive(name)[0]
    ):
        raise ValueError(f"Unsafe entry name in remote directory: {name!r}")
    return name


def _assert_safe_local_child(real_root: str, child_local: str) -> None:
    if os.path.islink(child_local):
        raise ValueError(f"Refusing to overwrite local symlink: {child_local}")
    real_child = os.path.realpath(child_local)
    try:
        common = os.path.commonpath([real_root, real_child])
    except ValueError as e:
        raise ValueError(f"Unsafe destination path: {child_local}") from e
    if common != real_root or real_child == real_root:
        raise ValueError(f"Unsafe destination path: {child_local}")


class ContentsClient:
    def __init__(self, session_state: SessionState):
        self.base_url = session_state.url.rstrip("/")
        self.token = session_state.token
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _request(
        self, method: str, path: str, params: dict = None, json_data: dict = None
    ):
        # Quote the path, but don't encode slashes so directory paths stay intact
        quoted_path = quote(path.strip("/"), safe="/")
        url = f"{self.base_url}/api/contents/{quoted_path}"

        req_params = {"authuser": "0", "colab-runtime-proxy-token": self.token}
        if params:
            req_params.update(params)

        response = self._session.request(method, url, params=req_params, json=json_data)

        if get_status_code(response) == 404:
            raise FileNotFoundError(f"File or directory not found: {path}")

        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            resp = e.response or response
            reason = getattr(resp, "reason", None) or "Error"
            raise requests.HTTPError(
                f"{resp.status_code} {reason} for url: {url}",
                response=resp,
            ) from None

        # DELETE doesn't return JSON
        if method == "DELETE":
            return None

        return response.json()

    def list_dir(self, path: str):
        return self._request("GET", path)

    def upload(self, local_path: str, remote_path: str):
        with open(local_path, "rb") as f:
            content = f.read()

        b64_content = base64.b64encode(content).decode("ascii")
        filename = remote_path.split("/")[-1]

        payload = {
            "name": filename,
            "path": remote_path,
            "type": "file",
            "format": "base64",
            "content": b64_content,
            "chunk": 1,
        }

        return self._request("PUT", remote_path, json_data=payload)

    def mkdir(self, remote_path: str):
        return self._request("PUT", remote_path, json_data={"type": "directory"})

    def _ensure_dir(self, remote_path: str) -> None:
        try:
            self.mkdir(remote_path)
        except (FileNotFoundError, requests.HTTPError) as e:
            if isinstance(e, requests.HTTPError) and get_status_code(e) != 500:
                raise
            parent = posixpath.dirname(remote_path)
            if not parent or parent == remote_path or parent == "content":
                raise
            self._ensure_dir(parent)
            self.mkdir(remote_path)

    def upload_dir(self, local_dir: str, remote_dir: str) -> int:
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"Not a directory: {local_dir}")
        base_remote = _normalize_remote_path(remote_dir)
        if base_remote:
            self._ensure_dir(base_remote)

        def _raise_walk_error(err: OSError) -> None:
            raise err

        count = 0
        for root, dirs, files in os.walk(
            local_dir, followlinks=False, onerror=_raise_walk_error
        ):
            dirs[:] = sorted(
                d for d in dirs if not os.path.islink(os.path.join(root, d))
            )
            files.sort()
            rel = os.path.relpath(root, local_dir)
            rel_posix = "" if rel == "." else rel.replace(os.sep, "/")
            if base_remote and rel_posix:
                current_remote = posixpath.join(base_remote, rel_posix)
            else:
                current_remote = base_remote or rel_posix
            for d in dirs:
                self.mkdir(posixpath.join(current_remote, d) if current_remote else d)
            for f in files:
                local_file = os.path.join(root, f)
                if not os.path.islink(local_file) and os.path.isfile(local_file):
                    remote_file = (
                        posixpath.join(current_remote, f) if current_remote else f
                    )
                    self.upload(local_file, remote_file)
                    count += 1
        return count

    def download(self, remote_path: str, local_path: str):
        data = self._request("GET", remote_path, params={"content": "1"})

        if data.get("type") == "directory":
            raise IsADirectoryError(
                f"Cannot download a directory: {remote_path}. "
                "Use -r/--recursive to download directories."
            )

        _write_file_content(data, local_path)

    def download_dir(
        self,
        remote_path: str,
        local_path: str,
        *,
        _depth: int = 0,
        _max_depth: int = 64,
    ) -> int:
        if _depth > _max_depth:
            raise RecursionError(
                f"Maximum directory depth ({_max_depth}) exceeded while downloading {remote_path!r}"
            )
        base_remote = _normalize_remote_path(remote_path)
        data = self._request("GET", base_remote, params={"content": "1"})
        if data.get("type") != "directory":
            if os.path.islink(local_path):
                raise ValueError(f"Refusing to overwrite local symlink: {local_path}")
            if os.path.isdir(local_path) or local_path.endswith(("/", os.sep)):
                target_name = _validate_entry_name(
                    data.get("name") or posixpath.basename(base_remote)
                )
                os.makedirs(local_path, exist_ok=True)
                target = os.path.join(local_path, target_name)
                _assert_safe_local_child(os.path.realpath(local_path), target)
                _write_file_content(data, target)
            else:
                parent = os.path.dirname(local_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                _write_file_content(data, local_path)
            return 1

        if os.path.islink(local_path):
            raise ValueError(f"Refusing to traverse local symlink: {local_path}")
        os.makedirs(local_path, exist_ok=True)
        real_root = os.path.realpath(local_path)
        count = 0
        for item in data.get("content") or []:
            item_name = _validate_entry_name(item.get("name", ""))
            child_remote = (
                posixpath.join(base_remote, item_name) if base_remote else item_name
            )
            child_local = os.path.join(local_path, item_name)
            _assert_safe_local_child(real_root, child_local)
            if item.get("type") == "directory":
                count += self.download_dir(
                    child_remote,
                    child_local,
                    _depth=_depth + 1,
                    _max_depth=_max_depth,
                )
            else:
                self.download(child_remote, child_local)
                count += 1
        return count

    def rm(self, remote_path: str):
        self._request("DELETE", remote_path)
