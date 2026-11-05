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

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from colab_cli.state import SessionState


class RecordingSession:
    """Stands in for the authorized HTTP session and records every request."""

    def __init__(self):
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        raise RuntimeError(f"unexpected request: {method} {url}")


def fresh_session(name, ep, attached):
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    fields = dict(token="t", url="u", token_expires_at=expires)
    return SessionState(name=name, endpoint=ep, attached=attached, **fields)


ATT = ("att", "att-ep", True)
DUP_CLI = ("dup", "dup-ep", False)


class UnassignSession(RecordingSession):
    """Records every request and accepts unassign calls."""

    def request(self, method, url, **kwargs):
        if "/unassign/" not in url:
            return super().request(method, url, **kwargs)
        self.requests.append((method, url))
        body = '{"token": "xsrf"}' if method == "GET" else ""
        return MagicMock(ok=True, status_code=200, reason="OK", text=body)


def use_guarded_client(st, mocker, session=None):
    st._client = None
    credentials = mocker.patch("colab_cli.common.get_credentials")
    credentials.return_value = session or RecordingSession()


def unassigned_endpoints(st):
    urls = [url for _, url in st.client.session.requests if "/unassign/" in url]
    return {url.rsplit("/", 1)[-1] for url in urls}
