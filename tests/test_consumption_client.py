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

import pytest
from unittest.mock import MagicMock

from colab_cli.client import Client, Prod
from colab_cli.consumption import format_consumption_status


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def client(mock_session):
    return Client(Prod(), mock_session)


def test_get_consumption_user_info_from_ccu_info(client, mock_session):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.text = ")]}'\n" + json.dumps(
        {
            "currentBalance": 298.53,
            "consumptionRateHourly": 0.07,
            "assignmentsCount": 2,
        }
    )
    mock_session.request.return_value = mock_resp

    info = client.get_consumption_user_info()

    assert info.paid_compute_units_balance == 298.53
    assert info.consumption_rate_hourly == 0.07
    assert info.assignments_count == 2
    mock_session.request.assert_called_once()
    call_args = mock_session.request.call_args
    assert call_args.args[1].endswith("/tun/m/ccu-info")
    assert call_args.kwargs["params"] == {"authuser": "0"}


def test_ccu_info_output_format(client, mock_session):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.text = ")]}'\n" + json.dumps(
        {
            "currentBalance": 298.53,
            "consumptionRateHourly": 0.0,
            "assignmentsCount": 0,
        }
    )
    mock_session.request.return_value = mock_resp

    info = client.get_consumption_user_info()
    output = format_consumption_status(info)
    assert "0.00/hr" in output
    assert "Current balance: 298.53 compute units" in output
    assert "Active assignments: 0" in output
