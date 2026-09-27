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

import json
import uuid
from unittest.mock import MagicMock

import pytest
import requests

from colab_cli.client import (
    Accelerator,
    Assignment,
    Client,
    ColabRequestError,
    GetAssignmentResponse,
    PostAssignmentResponse,
    Prod,
    Shape,
    TooManyAssignmentsError,
    Variant,
    resolve_assign_shape,
)


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def client(mock_session):
    return Client(Prod(), mock_session)


def test_request_timeout_override_and_explicit_precedence(
    client, mock_session, monkeypatch
):
    monkeypatch.setenv("COLAB_CLI_REQUEST_TIMEOUT", "300")
    mock_session.request.return_value.text = ""
    client._issue_request("https://colab.research.google.com/test")
    assert mock_session.request.call_args.kwargs["timeout"] == 300
    client._issue_request("https://colab.research.google.com/test", timeout=12)
    assert mock_session.request.call_args.kwargs["timeout"] == 12


def test_request_default_timeout_is_preserved(client, mock_session, monkeypatch):
    monkeypatch.delenv("COLAB_CLI_REQUEST_TIMEOUT", raising=False)
    mock_session.request.return_value.text = ""
    client._issue_request("https://colab.research.google.com/test")
    assert "timeout" not in mock_session.request.call_args.kwargs


def pending_assignment(token="initial"):
    return GetAssignmentResponse(acc="NONE", nbh="same", token=token, variant="DEFAULT")


def allocated_assignment():
    return Assignment(
        endpoint="recovered",
        runtimeProxyInfo={
            "token": "proxy",
            "tokenExpiresInSeconds": 3600,
            "url": "http://backend",
        },
    )


@pytest.mark.parametrize("status", [None, 502, 503, 504])
def test_assign_recovers_before_another_post(client, mocker, monkeypatch, status):
    monkeypatch.setenv("COLAB_CLI_ASSIGN_RETRIES", "3")
    monkeypatch.setenv("COLAB_CLI_RETRY_BACKOFF", "10")
    recovered = allocated_assignment()
    get = mocker.patch.object(
        client, "_get_assignment", side_effect=[pending_assignment(), recovered]
    )
    error = (
        requests.exceptions.ReadTimeout("allocation pending")
        if status is None
        else ColabRequestError(
            "transient", request=None, response=MagicMock(status_code=status)
        )
    )
    post = mocker.patch.object(client, "_post_assignment", side_effect=error)
    sleep = mocker.patch("time.sleep")
    notebook = uuid.uuid4()
    assert client.assign(notebook) is recovered
    assert get.call_count == 2
    assert all(call.args[0] == notebook for call in get.call_args_list)
    post.assert_called_once()
    sleep.assert_called_once_with(10)


def test_assign_refreshes_token_and_uses_bounded_backoff(client, mocker, monkeypatch):
    monkeypatch.setenv("COLAB_CLI_ASSIGN_RETRIES", "3")
    monkeypatch.setenv("COLAB_CLI_RETRY_BACKOFF", "10")
    mocker.patch.object(
        client,
        "_get_assignment",
        side_effect=[pending_assignment(str(i)) for i in range(4)],
    )
    post = mocker.patch.object(
        client,
        "_post_assignment",
        side_effect=requests.exceptions.ReadTimeout("pending"),
    )
    sleep = mocker.patch("time.sleep")
    notebook = uuid.uuid4()
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.assign(notebook)
    assert [call.args[:2] for call in post.call_args_list] == [
        (notebook, str(i)) for i in range(4)
    ]
    assert [call.args[0] for call in sleep.call_args_list] == [10, 20, 40]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_assign_does_not_retry_nontransient_errors(client, mocker, status):
    get = mocker.patch.object(
        client, "_get_assignment", return_value=pending_assignment()
    )
    post = mocker.patch.object(
        client,
        "_post_assignment",
        side_effect=ColabRequestError(
            "permanent", request=None, response=MagicMock(status_code=status)
        ),
    )
    sleep = mocker.patch("time.sleep")
    with pytest.raises(ColabRequestError):
        client.assign(uuid.uuid4())
    get.assert_called_once()
    post.assert_called_once()
    sleep.assert_not_called()


def test_assign_succeeds_after_recovery_refreshes_token(client, mocker, monkeypatch):
    monkeypatch.setenv("COLAB_CLI_ASSIGN_RETRIES", "1")
    monkeypatch.setenv("COLAB_CLI_RETRY_BACKOFF", "0")
    get = mocker.patch.object(
        client,
        "_get_assignment",
        side_effect=[pending_assignment("first"), pending_assignment("fresh")],
    )
    result = PostAssignmentResponse(
        accelerator="NONE",
        endpoint="new",
        variant=0,
        runtimeProxyInfo={
            "token": "proxy",
            "tokenExpiresInSeconds": 3600,
            "url": "http://backend",
        },
    )
    post = mocker.patch.object(
        client,
        "_post_assignment",
        side_effect=[requests.exceptions.ReadTimeout("pending"), result],
    )
    mocker.patch("time.sleep")
    notebook = uuid.uuid4()
    assert client.assign(notebook) is result
    assert get.call_count == 2
    assert [call.args[:2] for call in post.call_args_list] == [
        (notebook, "first"),
        (notebook, "fresh"),
    ]


def test_assign_retries_can_be_disabled(client, mocker, monkeypatch):
    monkeypatch.setenv("COLAB_CLI_ASSIGN_RETRIES", "0")
    get = mocker.patch.object(
        client, "_get_assignment", return_value=pending_assignment()
    )
    post = mocker.patch.object(
        client,
        "_post_assignment",
        side_effect=requests.exceptions.ReadTimeout("pending"),
    )
    sleep = mocker.patch("time.sleep")
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.assign(uuid.uuid4())
    get.assert_called_once()
    post.assert_called_once()
    sleep.assert_not_called()


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


def test_client_assign_412_raises_too_many_assignments(client, mock_session):
    """A 412 on the POST /assign step should surface as
    TooManyAssignmentsError, not the raw ColabRequestError."""
    get_resp = MagicMock()
    get_resp.ok = True
    get_resp.text = ")]}'\n" + json.dumps(
        {"acc": "NONE", "nbh": "some_nbh", "token": "xsrf_token", "variant": "DEFAULT"}
    )

    post_resp = MagicMock()
    post_resp.ok = False
    post_resp.status_code = 412
    post_resp.reason = "Precondition Failed"
    post_resp.text = "Precondition Failed"

    mock_session.request.side_effect = [get_resp, post_resp]

    with pytest.raises(TooManyAssignmentsError):
        client.assign(uuid.uuid4())


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


def test_client_list_assignments_forwards_request_timeout(client, mock_session):
    """Interactive reconnects can bound a stalled control-plane lookup."""
    resp = MagicMock()
    resp.ok = True
    resp.text = ")]}'\n" + json.dumps({"assignments": []})
    mock_session.request.return_value = resp

    assert client.list_assignments(timeout=10) == []
    assert mock_session.request.call_args.kwargs["timeout"] == 10


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


def test_client_assign_url_includes_shape_hm(client, mock_session):
    get_resp = MagicMock()
    get_resp.ok = True
    get_resp.text = ")]}'\n" + json.dumps(
        {"acc": "NONE", "nbh": "some_nbh", "token": "xsrf_token", "variant": "DEFAULT"}
    )
    post_resp = MagicMock()
    post_resp.ok = True
    post_resp.text = ")]}'\n" + json.dumps(
        {
            "accelerator": "A100",
            "endpoint": "new_endpoint",
            "runtimeProxyInfo": {
                "token": "proxy_token",
                "tokenExpiresInSeconds": 3600,
                "url": "http://backend",
            },
            "variant": 1,
        }
    )
    mock_session.request.side_effect = [get_resp, post_resp]

    client.assign(
        uuid.uuid4(),
        variant=Variant.GPU,
        accelerator=Accelerator.A100,
        shape=Shape.HIGH_RAM,
    )

    get_url = mock_session.request.call_args_list[0].args[1]
    assert "shape=hm" in get_url
    post_url = mock_session.request.call_args_list[1].args[1]
    assert "shape=hm" in post_url


def test_client_assign_url_omits_shape_for_standard(client, mock_session):
    get_resp = MagicMock()
    get_resp.ok = True
    get_resp.text = ")]}'\n" + json.dumps(
        {"acc": "NONE", "nbh": "some_nbh", "token": "xsrf_token", "variant": "DEFAULT"}
    )
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

    client.assign(uuid.uuid4())

    get_url = mock_session.request.call_args_list[0].args[1]
    assert "shape=" not in get_url


@pytest.mark.parametrize(
    "accelerator,high_mem,expected",
    [
        (Accelerator.T4, True, Shape.HIGH_RAM),
        (Accelerator.A100, True, Shape.HIGH_RAM),
        (Accelerator.NONE, True, Shape.HIGH_RAM),
        (Accelerator.L4, True, None),
        (Accelerator.V5E1, True, None),
        (Accelerator.T4, False, None),
    ],
)
def test_resolve_assign_shape(accelerator, high_mem, expected):
    assert resolve_assign_shape(accelerator, high_mem=high_mem) == expected
