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

"""Account-level compute-unit display for `colab usage`."""

from pydantic import BaseModel, ConfigDict, Field


class CcuInfo(BaseModel):
    """Response from ``GET /tun/m/ccu-info`` (session backend)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    current_balance: float = Field(..., alias="currentBalance")
    consumption_rate_hourly: float = Field(..., alias="consumptionRateHourly")
    assignments_count: int = Field(..., alias="assignmentsCount")


class ConsumptionUserInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    paid_compute_units_balance: float = Field(..., alias="paidComputeUnitsBalance")
    consumption_rate_hourly: float = Field(..., alias="consumptionRateHourly")
    assignments_count: int = Field(..., alias="assignmentsCount")


def consumption_user_info_from_tunnel(ccu: CcuInfo) -> ConsumptionUserInfo:
    """Map tunnel ccu-info into the display container."""
    return ConsumptionUserInfo(
        paid_compute_units_balance=ccu.current_balance,
        consumption_rate_hourly=ccu.consumption_rate_hourly,
        assignments_count=ccu.assignments_count,
    )


def format_consumption_status(info: ConsumptionUserInfo) -> str:
    """Balance, rate, and active-assignment count, one labeled line each."""
    balance = f"{info.paid_compute_units_balance:.2f}"
    rate = f"{info.consumption_rate_hourly:.2f}"
    count = f"{info.assignments_count:.0f}"
    return (
        f"Current balance: {balance} compute units\n"
        f"Usage rate: {rate}/hr\n"
        f"Active assignments: {count}"
    )
