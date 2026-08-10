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

"""Tests for the account-level compute-unit display shown by `colab usage`."""

from colab_cli.consumption import ConsumptionUserInfo, format_consumption_status


def _info(**kwargs) -> ConsumptionUserInfo:
    defaults = {
        "paid_compute_units_balance": 0.0,
        "consumption_rate_hourly": 0.07,
        "assignments_count": 0,
    }
    defaults.update(kwargs)
    return ConsumptionUserInfo(**defaults)


def test_format_consumption_status():
    info = _info(
        paid_compute_units_balance=298.53,
        consumption_rate_hourly=0.07,
        assignments_count=2,
    )
    assert format_consumption_status(info) == (
        "Current balance: 298.53 compute units\n"
        "Usage rate: 0.07/hr\n"
        "Active assignments: 2"
    )


def test_format_consumption_status_zero_balance_and_rate():
    info = _info(
        paid_compute_units_balance=0.0,
        consumption_rate_hourly=0.0,
        assignments_count=0,
    )
    assert format_consumption_status(info) == (
        "Current balance: 0.00 compute units\n"
        "Usage rate: 0.00/hr\n"
        "Active assignments: 0"
    )
