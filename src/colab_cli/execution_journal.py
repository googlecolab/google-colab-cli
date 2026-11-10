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

"""Read a request's recovery records through the existing Contents API."""

import importlib.resources
import json
import re
from urllib.parse import quote
import uuid

import requests


class ExecutionJournal:
    def __init__(self, url, token, request_id, path=None):
        self.request_id = request_id
        self.path = path or f"/content/.colab-cli-executions/{uuid.uuid4().hex}.jsonl"
        if not request_id or not re.fullmatch(
            r"/content/\.colab-cli-executions/[0-9a-f]{32}\.jsonl", self.path
        ):
            raise ValueError("Invalid execution recovery identity")
        self.update_connection(url, token)
        self._offset = 0
        self._sequence = 0

    def update_connection(self, url, token):
        """Keep the request and read position while refreshing its proxy route."""
        self.url = (
            f"{url.rstrip('/')}/api/contents/{quote(self.path.lstrip('/'), safe='/')}"
        )
        self.params = {"authuser": "0", "colab-runtime-proxy-token": token}

    def install_code(self):
        source = (
            importlib.resources.files("colab_cli")
            .joinpath("kernel_journal.py")
            .read_text(encoding="utf-8")
        )
        source += f"\ninstall(get_ipython().kernel.session, {self.request_id!r}, {self.path!r})"
        return f"exec({source!r}, {{'get_ipython': get_ipython}})"

    def read(self, timeout):
        response = requests.get(
            self.url,
            params={**self.params, "content": "1", "format": "text"},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("format") != "text"
            or not isinstance(data.get("content"), str)
        ):
            raise ValueError("Invalid execution recovery record")
        # A concurrent append may leave a partial last line in the snapshot.
        content = data["content"]
        end = content.rfind("\n") + 1
        if end < self._offset:
            raise ValueError("Execution recovery record was truncated")
        lines = content[self._offset : end].splitlines()
        messages = []
        for sequence, line in enumerate(lines, self._sequence + 1):
            msg = json.loads(line)
            if not isinstance(msg, dict) or any(
                not isinstance(msg.get(key), dict)
                for key in ("header", "parent_header", "content")
            ):
                raise ValueError("Invalid execution recovery message")
            if (
                msg.get("parent_header", {}).get("msg_id") != self.request_id
                or not msg.get("header", {}).get("msg_id")
                or not isinstance(msg["header"].get("msg_type"), str)
                or msg.get("_colab_cli_sequence") != sequence
            ):
                raise ValueError(
                    "Execution recovery record does not match the original request"
                )
            messages.append(msg)
        self._offset = end
        self._sequence += len(messages)
        return messages

    def remove(self):
        response = requests.delete(self.url, params=self.params, timeout=3)
        response.raise_for_status()
