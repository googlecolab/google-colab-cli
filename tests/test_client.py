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

import uuid
import json
import pytest
from unittest.mock import MagicMock
from colab_cli.client import Client, Prod, PostAssignmentResponse, Assignment


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def client(mock_session):
    return Client(Prod(), mock_session)


def test_client_assign_new(client, mock_session):
    # Mock _get_assignment (GET)
    get_resp = MagicMock()
    get_resp.ok = True
    get_resp.text = ")]}'\n" + json.dumps(
        {"acc": "NONE", "nbh": "some_nbh", "token": "xsrf_token", "variant": "DEFAULT"}
    )

    # Mock _post_assignment (POST)
    post_resp = MagicMock()
    post_resp.ok = True
    post_resp.text = ")]}'\n" + json.dumps(
        {
            "accelerator": "NONE",
            "endpoint": "new_endpoint",
            "runtimeProxyInfo": {
                "token": "proxy_token",
                "tokenExpiresInSeconds": 3600,
                "url": "http://backend",
            },
            "variant": 0,
        }
    )

    mock_session.request.side_effect = [get_resp, post_resp]

    res = client.assign(uuid.uuid4())

    assert isinstance(res, PostAssignmentResponse)
    assert res.endpoint == "new_endpoint"
    assert res.runtime_proxy_info.token == "proxy_token"

    # Check POST request headers for XSRF token
    assert mock_session.request.call_count == 2
    last_call_args = mock_session.request.call_args_list[1]
    assert last_call_args.kwargs["headers"]["X-Goog-Colab-Token"] == "xsrf_token"


def test_client_unassign(client, mock_session):
    # Mock GET for XSRF token
    get_resp = MagicMock()
    get_resp.ok = True
    get_resp.text = ")]}'\n" + json.dumps({"token": "unassign_xsrf_token"})

    # Mock POST for unassign
    post_resp = MagicMock()
    post_resp.ok = True
    post_resp.text = ""  # 204 No Content typically

    mock_session.request.side_effect = [get_resp, post_resp]

    client.unassign("my_endpoint")

    assert mock_session.request.call_count == 2
    last_call_args = mock_session.request.call_args_list[1]
    assert (
        last_call_args.kwargs["headers"]["X-Goog-Colab-Token"] == "unassign_xsrf_token"
    )
    assert "unassign/my_endpoint" in last_call_args.args[1]


def test_client_assign_existing(client, mock_session):
    # Mock _get_assignment (GET) returning existing Assignment
    get_resp = MagicMock()
    get_resp.ok = True
    get_resp.text = ")]}'\n" + json.dumps(
        {
            "endpoint": "existing_endpoint",
            "runtimeProxyInfo": {
                "token": "existing_token",
                "tokenExpiresInSeconds": 3600,
                "url": "http://existing-backend",
            },
        }
    )

    mock_session.request.return_value = get_resp

    res = client.assign(uuid.uuid4())

    assert isinstance(res, Assignment)
    assert res.endpoint == "existing_endpoint"
    assert mock_session.request.call_count == 1


def test_client_list_assignments(client, mock_session):
    # Mock list_assignments (GET)
    resp = MagicMock()
    resp.ok = True
    resp.text = ")]}'\n" + json.dumps(
        {
            "assignments": [
                {
                    "accelerator": "NONE",
                    "endpoint": "e1",
                    "variant": 0,
                    "machineShape": 0,
                    "runtimeProxyInfo": {
                        "token": "t1",
                        "tokenExpiresInSeconds": 3600,
                        "url": "u1",
                    },
                }
            ]
        }
    )

    mock_session.request.return_value = resp

    # This should fail if list_assignments is not implemented
    res = client.list_assignments()

    assert len(res) == 1
    assert res[0].endpoint == "e1"
    assert "tun/m/assignments" in mock_session.request.call_args.args[1]


def test_client_keep_alive_assignment_handles_empty_response(client, mock_session):
    """The tunnel keep-alive ping returns an empty body. With no `schema=`,
    _issue_request must short-circuit and not attempt to parse it."""
    resp = MagicMock()
    resp.ok = True
    resp.text = ""
    mock_session.request.return_value = resp

    # Should NOT raise.
    result = client.keep_alive_assignment("m-s-test")
    assert result is None  # no schema, so no return value


def test_client_keep_alive_assignment_request_shape(client, mock_session):
    """Keep-alive is a Tunnel Frontend (TFE) HTTP ping, NOT the
    `colab.pa.googleapis.com` RuntimeService RPC.

    Background: the RuntimeService RPC requires the caller to be a
    serviceusage consumer of Colab's internal project (1014160490159), which
    no ordinary user account is. That path returned HTTP 403
    USER_PROJECT_DENIED for every external user (issue #14). The official
    Colab clients (and the colab-vscode extension) keep assignments alive via
    a TFE-intercepted GET that only needs the user's own Gaia bearer token:

        GET https://colab.research.google.com/tun/m/<endpoint>/keep-alive/
        X-Colab-Tunnel: Google

    TFE records LastActiveTime before forwarding, so the request keeps the VM
    from being idle-pruned. This test pins that wire format.
    """
    resp = MagicMock()
    resp.ok = True
    resp.text = ""
    mock_session.request.return_value = resp

    client.keep_alive_assignment("m-s-test-endpoint")

    assert mock_session.request.call_count == 1
    call = mock_session.request.call_args
    method, url = call.args[0], call.args[1]
    headers = call.kwargs["headers"]

    assert method == "GET"
    # TFE tunnel keep-alive path on the session backend host.
    assert url.endswith("/tun/m/m-s-test-endpoint/keep-alive/")
    assert "colab.research.google.com" in url
    # The request must be resolved through the Colab tunnel; without this
    # header the front-door rejects the request with HTTP 400.
    assert headers["X-Colab-Tunnel"] == "Google"
    # Must NOT hit the RuntimeService / pa.googleapis.com path anymore.
    assert "pa.googleapis.com" not in url
    assert "KeepAliveAssignment" not in url
    # No fire-and-forget JSON body; this is a plain GET.
    assert "json" not in call.kwargs
    # A short timeout is supplied so the daemon stays responsive on its cadence.
    assert call.kwargs.get("timeout") is not None


def test_client_keep_alive_assignment_treats_read_timeout_as_success(
    client, mock_session
):
    """TFE records activity as soon as the request arrives, then forwards to a
    VM that may not respond — so the request commonly read-times-out even
    though the keep-alive succeeded. A ReadTimeout must NOT propagate as an
    error (otherwise the daemon would log spurious keep_alive_error events)."""
    import requests

    mock_session.request.side_effect = requests.exceptions.ReadTimeout("timed out")

    # Should NOT raise.
    result = client.keep_alive_assignment("m-s-test-endpoint")
    assert result is None


def test_client_keep_alive_assignment_propagates_http_error(client, mock_session):
    """A genuine HTTP error (e.g. 404 for a deleted assignment) must still
    surface so the daemon can react (e.g. stop after consecutive 4xx)."""
    from colab_cli.client import ColabRequestError

    resp = MagicMock()
    resp.ok = False
    resp.status_code = 404
    resp.reason = "Not Found"
    resp.text = "gone"
    mock_session.request.return_value = resp

    with pytest.raises(ColabRequestError):
        client.keep_alive_assignment("m-s-test-endpoint")
