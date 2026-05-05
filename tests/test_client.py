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


def test_client_keep_alive_assignment_handles_empty_array_response(
    client, mock_session
):
    """KeepAliveAssignment returns `[]` on success (Boq protojson). When the
    caller passes no `schema=`, _issue_request must not try to validate the
    body — otherwise it raises pydantic ValidationError on the empty list.
    Regression: discovered live 2026-04-30."""
    resp = MagicMock()
    resp.ok = True
    resp.text = "[]"
    mock_session.request.return_value = resp

    # Should NOT raise.
    result = client.keep_alive_assignment("m-s-test")
    assert result is None  # no schema, so no return value


def test_client_keep_alive_assignment_request_shape(client, mock_session):
    """The Boq RuntimeService rejects the request with HTTP 400 unless
    `X-Goog-Api-Client` contains `grpc-web`. This test pins the wire format
    that talks to colab.pa.googleapis.com.
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
    body = call.kwargs["json"]

    assert method == "POST"
    assert url.endswith(
        "/$rpc/google.internal.colab.v1.RuntimeService/KeepAliveAssignment"
    )
    # Positional protojson encoding: a single-element array with the endpoint.
    assert body == ["m-s-test-endpoint"]
    assert headers["Content-Type"] == "application/json+protobuf"
    assert "x-goog-api-key" in headers
    assert headers["x-user-agent"] == "grpc-web-javascript/0.1"
    # Critical: server requires `grpc-web` substring in this header.
    assert "grpc-web" in headers["x-goog-api-client"]
    # Critical: pin consumer project to Colab's, otherwise ADC user creds
    # (which carry their own gcloud quota project) trigger HTTP 400
    # CONSUMER_INVALID. Verified empirically 2026-04-30.
    assert headers["x-goog-user-project"] == "1014160490159"
