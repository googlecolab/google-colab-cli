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

"""Session records, fake HTTP sessions and store helpers shared by tests."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from colab_cli.state import SessionState

ATT = ("att", "att-ep", True)
ATT2 = ("att2", "att2-ep", True)
CLI = ("cli", "cli-ep", False)
SHARED = ("cli2", "att-ep", False)


class RecordingSession:
    """Stands in for the authorized HTTP session and records every request."""

    def __init__(self):
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        raise RuntimeError(f"unexpected request: {method} {url}")


class UnassignSession(RecordingSession):
    """Records every request and accepts unassign calls."""

    def request(self, method, url, **kwargs):
        if "/unassign/" not in url:
            return super().request(method, url, **kwargs)
        self.requests.append((method, url))
        body = '{"token": "xsrf"}' if method == "GET" else ""
        return MagicMock(ok=True, status_code=200, reason="OK", text=body)


class AssignmentsSession(UnassignSession):
    """Records requests, lists `endpoints` as assigned and accepts unassign."""

    def __init__(self, *endpoints: str):
        super().__init__()
        self.endpoints = endpoints

    def request(self, method, url, **kwargs):
        if not url.endswith("/tun/m/assignments"):
            return super().request(method, url, **kwargs)
        self.requests.append((method, url))
        info = {"token": "t", "tokenExpiresInSeconds": 3600}
        listed = [
            {
                "accelerator": "NONE",
                "endpoint": ep,
                "variant": 0,
                "machineShape": 0,
                "runtimeProxyInfo": dict(info, url=f"https://{ep}"),
            }
            for ep in self.endpoints
        ]
        text = json.dumps({"assignments": listed})
        return MagicMock(ok=True, status_code=200, reason="OK", text=text)


def fresh_session(name: str, endpoint: str, attached: bool) -> SessionState:
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    return SessionState(
        name=name,
        token="t",
        url=f"https://{endpoint}",
        endpoint=endpoint,
        token_expires_at=expires,
        attached=attached,
    )


def use_guarded_client(local_state, mocker, http_session=None):
    local_state._client = None
    credentials = mocker.patch("colab_cli.common.get_credentials")
    credentials.return_value = http_session or RecordingSession()


def unassigned_endpoints(local_state) -> set[str]:
    urls = [
        url for _, url in local_state.client.session.requests if "/unassign/" in url
    ]
    return {url.rsplit("/", 1)[-1] for url in urls}
